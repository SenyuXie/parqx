"""Deliver the session's active query to the workspace that submitted it."""

from dataclasses import dataclass
from functools import partial
from typing import Any
from weakref import ReferenceType, ref

from textual.app import App
from textual.worker import Worker, WorkerState

from parqx.catalog import SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.query import QueryExecutionError, QueryResult, execute_query
from parqx.tui.query.workspace import QueryWorkspace


@dataclass
class _Submission:
    """Hold a workspace and its outcome until UI delivery or session shutdown."""

    workspace: QueryWorkspace
    outcome: QueryResult | str | None = None


def _execute(
    destination: ReferenceType[_Submission],
    sql: str,
    sources: tuple[SourceSpec, ...],
    unavailable: tuple[SourceIssue, ...],
    config: ParqxConfig,
) -> None:
    """Publish the native outcome if its submission still exists.

    Args:
        destination: Weak reference to the session-owned submission.
        sql: SQL draft captured by the submitting workspace.
        sources: Ready sources to register for this execution.
        unavailable: Source warnings captured with the ready sources.
        config: Preview and execution budgets for the native query.
    """
    try:
        outcome: QueryResult | str = execute_query(
            sources, sql, config=config, unavailable=unavailable
        )
    except QueryExecutionError as error:
        # Tracebacks can retain partial Arrow batches from a failed preview.
        outcome = str(error) or type(error).__name__
        if error.issues:
            outcome += "\nUnavailable sources:\n" + "\n".join(map(str, error.issues))

    # Textual retains the last worker in its executor thread. Only the session
    # owns the submission, so shutdown releases even an undelivered preview.
    if (submission := destination()) is not None:
        submission.outcome = outcome


class QueryDelivery:
    """Keep one query in flight through delivery to its submitting workspace."""

    def __init__(self, app: App[Any], config: ParqxConfig) -> None:
        """Start idle with the session's query budgets.

        Args:
            app: The Textual app whose worker manager runs SQL queries.
            config: Preview and execution budgets for this session.
        """
        self._app = app
        self._config = config
        self._active: tuple[Worker[None], _Submission] | None = None

    def submit(
        self,
        request: QueryWorkspace.RunRequested,
        sources: tuple[SourceSpec, ...],
        unavailable: tuple[SourceIssue, ...],
    ) -> bool:
        """Start a query and lock its workspace on the UI thread.

        Args:
            request: The workspace and SQL draft captured on submission.
            sources: Ready sources at the time of submission.
            unavailable: Availability warnings for the same query.

        Returns:
            True if the session accepted the query, or False if it is busy.
        """
        if self._active is not None:
            return False
        workspace = request.workspace
        submission = _Submission(workspace)
        worker = self._app.run_worker(
            partial(
                _execute,
                ref(submission),
                request.sql,
                sources,
                unavailable,
                self._config,
            ),
            group="query",
            description="Run SQL query",
            thread=True,
            exit_on_error=False,
        )
        self._active = (worker, submission)
        workspace.running = True
        return True

    async def deliver(self, event: Worker.StateChanged) -> None:
        """Apply the active worker's completion before unlocking its workspace.

        Args:
            event: A worker state change received by the app on the UI thread.
        """
        active = self._active
        if active is None:
            return
        worker, submission = active
        if event.worker is not worker:  # pyright: ignore[reportUnknownMemberType]
            return
        if event.state not in {WorkerState.SUCCESS, WorkerState.ERROR}:
            return
        workspace = submission.workspace
        try:
            if not workspace.is_attached:
                return
            outcome = submission.outcome
            if (error := worker.error) is not None:
                outcome = str(error) or type(error).__name__
            if isinstance(outcome, QueryResult):
                await workspace.show_result(outcome)
            elif outcome is not None:
                self._app.notify(
                    outcome,
                    title="SQL error",
                    severity="error",
                    timeout=4,
                    markup=False,
                )
        finally:
            if self._active is active:
                self._active = None
                if workspace.is_attached:
                    workspace.running = False

    def discard(self) -> None:
        """Drop the pending delivery at shutdown and let native SQL finish."""
        self._active = None
