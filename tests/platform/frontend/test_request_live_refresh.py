from tests.support.paths import REPOSITORY_ROOT


def test_request_monitor_polls_list_totals_trend_and_reconciled_detail_when_visible() -> None:
    source = (
        REPOSITORY_ROOT / "frontend/src/data-sources/apim/pages/dashboard-page.tsx"
    ).read_text()
    trace = source.split("function RequestTrace({", 1)[1].split("function RequestTraceDetail(", 1)[
        0
    ]
    assert "refetchInterval: 10_000" in trace
    assert "refetchIntervalInBackground: false" in trace
    assert trace.count("...liveRefresh") == 4
    assert "近实时，10秒刷新" in trace
    assert 'finopsQueries.usageActivity(filters, "day", timezone)' in trace
    assert "timeZone: timezone" in trace


def test_enabled_directory_filter_keeps_unattributed_subscription_calls() -> None:
    source = (REPOSITORY_ROOT / "frontend/src/lib/usage-filter-scope.ts").read_text()
    assert 'managed.includes("active") ? [...managed, "unattributed"]' in source
