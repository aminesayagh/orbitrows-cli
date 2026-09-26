from orbitrows.app import OrbitRowsApp


async def test_app_starts_and_quits():
    app = OrbitRowsApp()
    async with app.run_test() as pilot:
        assert app.query_one("#status")
        await pilot.press("q")
    assert app.return_code == 0
