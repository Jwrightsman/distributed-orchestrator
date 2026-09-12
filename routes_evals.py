"""The eval record, for the console's Evals view."""

from fastapi import APIRouter

import evals_view

router = APIRouter()


@router.get("/evals")
def evals():
    """What the eval harness recorded, and what it can resolve. No pass rate.

    **Viewer-gated, and deliberately not under `/v1/operator/`.** That prefix
    is for facts that should not reach the public edge; every byte this reads
    is committed to a public repository. It is not in `_PUBLIC_EXACT` either:
    the view it serves lives in the console.

    **`recorded: false` is not an empty record.** The deployed image carries no
    `evals/`, so a coordinator there has nothing to read, and saying "no runs
    yet" would claim the harness had never been run.

    A plain `def`, so the handful of file reads run on the threadpool rather
    than on the event loop. The record is small and nothing polls this: the
    view asks when it is opened.
    """
    record = evals_view.build_record()
    return {**record, "evals_html": evals_view.render(record)}
