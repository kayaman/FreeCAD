# SPDX-License-Identifier: LGPL-2.1-or-later
"""Document sessions, task identity and the task state machine.

This module has no FreeCAD or Qt imports. A callback from a provider or worker
is accepted only while its session, task, request and state still match, so a
late response can never mutate a model after Stop, New chat, a provider change,
a document switch or a document close.
"""

from dataclasses import dataclass, field
import enum
import itertools
import uuid

from .core import Conversation


class TaskState(str, enum.Enum):
    IDLE = "idle"
    CHECKING_PROVIDER = "checking provider"
    THINKING = "thinking"
    AWAITING_REVIEW = "awaiting review"
    EXECUTING = "executing"
    APPLYING = "applying"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


S = TaskState
TERMINAL = frozenset({S.COMPLETED, S.STOPPED, S.FAILED})
# States in which work is in flight and Stop is meaningful.
RUNNING = frozenset({S.CHECKING_PROVIDER, S.THINKING, S.EXECUTING, S.APPLYING})
_ENDINGS = {S.STOPPED, S.FAILED, S.PAUSED}
TRANSITIONS = {
    S.IDLE: {S.CHECKING_PROVIDER, S.THINKING} | _ENDINGS,
    S.CHECKING_PROVIDER: {S.THINKING} | _ENDINGS,
    S.THINKING: {S.AWAITING_REVIEW, S.EXECUTING, S.COMPLETED} | _ENDINGS,
    S.AWAITING_REVIEW: {S.EXECUTING} | _ENDINGS,
    S.EXECUTING: {S.APPLYING, S.CHECKING_PROVIDER, S.THINKING, S.AWAITING_REVIEW} | _ENDINGS,
    S.APPLYING: {S.CHECKING_PROVIDER, S.THINKING, S.COMPLETED} | _ENDINGS,
    S.PAUSED: {S.CHECKING_PROVIDER, S.THINKING, S.AWAITING_REVIEW, S.EXECUTING,
               S.STOPPED, S.FAILED},
    S.COMPLETED: set(),
    S.STOPPED: set(),
    S.FAILED: set(),
}


class Outcome(str, enum.Enum):
    PROPOSED = "proposed"
    REJECTED = "rejected"      # Never ran: stale model, unsupported, or refused.
    CANCELLED = "cancelled"    # Never ran: stopped before execution.
    FAILED = "failed"          # Ran and was rolled back.
    EXECUTED = "executed"      # Ran and was committed.
    UNDONE = "undone"          # Executed, then undone from the panel.


NOT_EXECUTED = frozenset({Outcome.PROPOSED, Outcome.REJECTED, Outcome.CANCELLED})


class StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Token:
    session: str
    task: str
    request: int


@dataclass
class Step:
    id: str
    request: int
    message: str
    code: str
    source_revision: str = ""
    outcome: Outcome = Outcome.PROPOSED
    result: str = ""
    diagnostics: dict = field(default_factory=dict)
    transaction: str = ""
    changed: tuple = ()
    revision_after: str = ""
    undo_count_after: int = -1
    properties: dict = field(default_factory=dict)   # Object -> changed property names.
    added: tuple = ()                                 # Objects the step created.


class Task:
    def __init__(self, session_id, goal, goal_message_id=""):
        self.id = uuid.uuid4().hex[:8]
        self.session_id = session_id
        self.goal = goal
        self.goal_message_id = goal_message_id
        self.state = S.IDLE
        self.steps = []
        self.request = 0          # Current request id; 0 means none is accepted.
        self._requests = itertools.count(1)
        self.history = [S.IDLE]
        self.snapshot = None      # Opaque document snapshot owned by the GUI.
        self.context = None       # A model context captured for the next request.

    def transition(self, state):
        state = TaskState(state)
        if state is self.state:
            return
        if state not in TRANSITIONS[self.state]:
            raise StateError("Cannot go from {} to {}.".format(self.state.value, state.value))
        self.state = state
        self.history.append(state)

    @property
    def finished(self):
        return self.state in TERMINAL

    @property
    def running(self):
        return self.state in RUNNING

    def new_request(self):
        self.request = next(self._requests)
        return Token(self.session_id, self.id, self.request)

    def invalidate(self):
        """Reject every outstanding callback before cancellation starts."""
        self.request = 0

    def add_step(self, request, message, code, source_revision=""):
        step = Step("{}/{}".format(self.id, len(self.steps) + 1), request, message, code,
                    source_revision)
        self.steps.append(step)
        return step

    @property
    def pending_step(self):
        if self.steps and self.steps[-1].outcome is Outcome.PROPOSED:
            return self.steps[-1]
        return None

    def close_pending(self, outcome, result=""):
        """Record why a proposal never ran; it must not look executed later."""
        step = self.pending_step
        if step is not None:
            step.outcome = Outcome(outcome)
            step.result = result
        return step

    def executed_steps(self):
        return [step for step in self.steps if step.outcome is Outcome.EXECUTED]


class UndoRefused(StateError):
    """Undo would not restore exactly the assistant's own changes; nothing changed."""


NORMAL_UNDO = " Nothing was changed. Use FreeCAD's normal Undo (Edit > Undo) to choose what to undo."


def plan_undo(task, scope, same_document, current_revision, undo_names):
    """Steps to undo, newest first, or UndoRefused.

    Undo is allowed only while the assistant's transactions form the current,
    uninterrupted tail of the document's undo history, nothing changed since the
    last of them, and FreeCAD still holds every transaction to be undone.
    """
    executed = task.executed_steps() if task is not None else []
    if not same_document or not executed:
        raise UndoRefused("No assistant step to undo in the active document.")
    targets = list(reversed(executed[-1:] if scope == "step" else executed))
    if current_revision != executed[-1].revision_after:
        raise UndoRefused("The document changed after the assistant's last step." + NORMAL_UNDO)
    expected = [step.transaction for step in targets]
    available = list(undo_names)
    if available[:len(expected)] != expected:
        if len(available) < len(expected) and available == expected[:len(available)]:
            raise UndoRefused("FreeCAD's undo history no longer holds every step of this task."
                              + NORMAL_UNDO)
        raise UndoRefused("Other changes are mixed into the undo history after the assistant's "
                          "steps." + NORMAL_UNDO)
    return targets


class DocumentSession:
    """Assistant state for one open document. Retired when the document closes."""

    def __init__(self, document=None):
        self.id = uuid.uuid4().hex[:12]
        self.document = document
        self.conversation = Conversation()
        self.tasks = []
        self.transcript = []
        self.retired = False

    @property
    def task(self):
        return self.tasks[-1] if self.tasks else None

    def start_task(self, goal, goal_message_id=""):
        current = self.task
        if current is not None and current.running:
            raise StateError("A task is already running.")
        if current is not None and not current.finished:
            # A paused or reviewable task is superseded by the new request.
            current.invalidate()
            step = current.close_pending(Outcome.CANCELLED, "Superseded by a new request.")
            if step is not None:
                self.conversation.log_step(step.id, step.outcome.value, step.message)
            current.transition(S.STOPPED)
        task = Task(self.id, goal, goal_message_id)
        self.tasks.append(task)
        return task

    def accepts(self, token, *states):
        task = self.task
        return (not self.retired and task is not None and token is not None
                and token.session == self.id and token.task == task.id
                and token.request != 0 and token.request == task.request
                and (not states or task.state in states))

    def undo_candidate(self):
        """The most recent task that still has executed steps."""
        for task in reversed(self.tasks):
            if task.executed_steps():
                return task
        return None

    def log(self, speaker, text):
        self.transcript.append((speaker, text))
        del self.transcript[:-500]

    def reset(self):
        """New chat: forget the conversation, design memory and task history."""
        if self.task is not None:
            self.task.invalidate()
        self.conversation = Conversation()
        self.tasks = []
        self.transcript = []


class SessionRegistry:
    """Maps live document objects to sessions; identity survives renaming."""

    def __init__(self):
        self.sessions = []

    def for_document(self, document):
        for session in self.sessions:
            if not session.retired and session.document is document:
                return session
        session = DocumentSession(document)
        self.sessions.append(session)
        return session

    def bind(self, session, document):
        """Attach an unbound (no-document) session to the document it created."""
        if session.document is not None and session.document is not document:
            raise StateError("Session already belongs to another document.")
        for other in self.sessions:
            if other is not session and not other.retired and other.document is document:
                other.retired = True
                if other.task is not None:
                    other.task.invalidate()
        session.document = document

    def retire(self, document):
        retired = []
        for session in self.sessions:
            if not session.retired and session.document is document:
                session.retired = True
                if session.task is not None:
                    session.task.invalidate()
                retired.append(session)
        self.sessions = [s for s in self.sessions if not s.retired]
        return retired
