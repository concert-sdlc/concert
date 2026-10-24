"""An issue travels from inbox to staging with a real PM loop and a scripted worker.

Nothing here writes events or handoffs by hand: the PM assigns, the worker executes and
reports, humans only type the commands the kits ask for, and CI is a check run on the head.
"""

from dataclasses import replace

from concert.core.counters import AgentState
from concert.core.transitions import Status
from concert.runner.cycle import run_cycle
from concert.runner.worker import AgentResult, Worker
from tests.runner.helpers import ALICE, CONFIG_AUTO, PM, VM1, World, spec_files
from tests.runner.test_evidence import POLICY
from tests.runner.test_worker import Scripted


def agent_for(issue: int) -> Scripted:
    return Scripted(
        {
            "triage": AgentResult(exit="spec"),
            "spec": AgentResult(exit="ready_for_approval", changes=spec_files(issue)),
            "implementing": AgentResult(
                exit="done", changes={"backend/export.py": "def export(): ..."}
            ),
            "in_review": AgentResult(exit="clean"),
            "uat": AgentResult(exit="passed"),
            "integrating": AgentResult(exit="ready_to_merge"),
            "verifying_staging": AgentResult(exit="passed"),
        }
    )


class Project:
    def __init__(self) -> None:
        self.world = World()
        self.world.config = replace(
            self.world.config, runners=CONFIG_AUTO.runners, gate_policy=POLICY
        )
        self.issue = self.world.new_issue()
        self.agent = agent_for(self.issue)
        self.worker = Worker(self.world.fake.client(VM1), "vm-1", VM1, PM, self.agent)

    def step(self) -> None:
        w = self.world
        w.fake.tick(1)
        w.heartbeat()
        run_cycle(w.pm, w.mirror, [self.issue], w.config, w.facts, now=w.fake.now)
        self.worker.run_once(self.issue, now=w.fake.now)

    def run_until(self, status: Status, limit: int = 40) -> None:
        for _ in range(limit):
            if self.status() is status:
                return
            self.step()
        raise AssertionError(f"stuck in {self.status()} waiting for {status}")

    def status(self) -> Status:
        return self.world.state(self.issue).status

    def human(self, text: str) -> None:
        self.world.say(ALICE, self.issue, text)


def test_an_issue_goes_from_inbox_to_staging() -> None:
    project = Project()
    world, issue = project.world, project.issue
    project.step()  # adopts the issue
    project.human("/dispatch")
    project.run_until(Status.SPEC_APPROVAL)
    project.human(f"/approve spec {world.state(issue).spec_rev}")
    project.run_until(Status.HUMAN_UAT)
    head = world.state(issue).head_sha or ""
    world.fake.add_check_run(head, "tests", "success", "w1")
    project.step()  # records the CI result and posts the UAT kit
    project.human(f"/approve uat {head}")
    project.run_until(Status.ON_STAGING)
    staging = world.pm.get_ref("staging") or ""
    assert world.pm.read_tree(staging)["backend/export.py"] == "def export(): ..."
    assert world.pm.get_status(issue) == "on_staging"
    states = [task.state for task in project.agent.tasks]
    assert states[:3] == ["triage", "spec", "implementing"]
    assert states.count("in_review") == 2 and states[-1] == "verifying_staging"


class Sequenced(Scripted):
    """Answers each state from a queue; the last answer repeats."""

    def __init__(self, base: Scripted, **queues: list[AgentResult]) -> None:
        super().__init__(base.results)
        self.queues = queues

    def run(self, task):  # type: ignore[no-untyped-def]
        queue = self.queues.get(task.state)
        if queue:
            self.tasks.append(task)
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return super().run(task)


def test_a_review_that_asks_for_changes_sends_the_issue_back_and_it_still_arrives() -> None:
    project = Project()
    project.agent = Sequenced(
        project.agent,
        in_review=[AgentResult(exit="changes_needed")] * 2 + [AgentResult(exit="clean")],
        implementing=[
            AgentResult(exit="done", changes={"backend/export.py": "v1"}),
            AgentResult(exit="done", changes={"backend/export.py": "v2"}),
        ],
    )
    project.worker.agent = project.agent
    world, issue = project.world, project.issue
    project.step()
    project.human("/dispatch")
    project.run_until(Status.SPEC_APPROVAL)
    project.human(f"/approve spec {world.state(issue).spec_rev}")
    project.run_until(Status.HUMAN_UAT)
    assert (
        world.state(issue).counters.used(AgentState.IMPLEMENTING) == 1
    )  # one return, charged once
    head = world.state(issue).head_sha or ""
    world.fake.add_check_run(head, "tests", "success", "w1")
    project.step()
    project.human(f"/approve uat {head}")
    project.run_until(Status.ON_STAGING)
    staging = world.pm.get_ref("staging") or ""
    assert world.pm.read_tree(staging)["backend/export.py"] == "v2"


def test_a_merge_that_breaks_staging_is_reverted_and_the_issue_lands_again() -> None:
    project = Project()
    project.agent = Sequenced(
        project.agent,
        # fail, re-run fails, the revert verifies, then the re-landed work verifies
        verifying_staging=[
            AgentResult(exit="failed"),
            AgentResult(exit="failed"),
            AgentResult(exit="passed"),
            AgentResult(exit="passed"),
        ],
        implementing=[
            AgentResult(exit="done", changes={"backend/export.py": "broken"}),
            AgentResult(exit="done", changes={"backend/export.py": "fixed"}),
        ],
    )
    project.worker.agent = project.agent
    project.world.config = replace(project.world.config, on_failure="auto_revert")
    world, issue = project.world, project.issue

    def approve_new_head() -> str:
        head = world.state(issue).head_sha or ""
        world.fake.add_check_run(head, "tests", "success", "w1")
        project.step()
        project.human(f"/approve uat {head}")
        return head

    project.step()
    project.human("/dispatch")
    project.run_until(Status.SPEC_APPROVAL)
    project.human(f"/approve spec {world.state(issue).spec_rev}")
    project.run_until(Status.HUMAN_UAT)
    approve_new_head()

    # the broken change merges, fails verification twice and is reverted
    for _ in range(40):
        state = world.state(issue)
        if state.revert_head is not None and state.revert_ci_passed is None:
            world.fake.add_check_run(state.revert_head, "tests", "success", "w1")
        if state.status is Status.IMPLEMENTING and state.relands == 1:
            break
        project.step()
    state = world.state(issue)
    assert state.status is Status.IMPLEMENTING and state.relands == 1
    staging = world.pm.get_ref("staging") or ""
    assert "backend/export.py" not in world.pm.read_tree(staging)  # staging is clean again

    # the fixed work goes through the whole gate again on the new branch
    project.run_until(Status.HUMAN_UAT)
    assert world.state(issue).branch == f"pm/{issue}-r1"
    approve_new_head()
    project.run_until(Status.ON_STAGING)
    staging = world.pm.get_ref("staging") or ""
    assert world.pm.read_tree(staging)["backend/export.py"] == "fixed"
