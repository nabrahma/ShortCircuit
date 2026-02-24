import pytest

from eod_analyzer import EODAnalyzer


@pytest.mark.asyncio
async def test_eod_analyzer_raises_when_db_none():
    analyzer = EODAnalyzer(db_manager=None)
    with pytest.raises(RuntimeError, match="db is None"):
        await analyzer.run_daily_analysis("2099-01-01")


@pytest.mark.asyncio
async def test_eod_analyzer_raises_when_query_missing():
    class NoQueryDB:
        pass

    analyzer = EODAnalyzer(db_manager=NoQueryDB())
    with pytest.raises(RuntimeError, match="missing .query"):
        await analyzer.run_daily_analysis("2099-01-01")
