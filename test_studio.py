"""Browser regression: auth, export and HTML/attribute injection in the Studio.

Install the optional browser-tests extra and run `playwright install chromium`.
All browser requests are routed to the isolated TestClient; no live server needed.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

playwright = pytest.importorskip("playwright.sync_api")


def test_studio_auth_export_and_untrusted_content(tmp_path):
    import server

    malicious = '<img src=x onerror="window.__xss=1">\'</textarea><svg onload="window.__xss=1">'
    run = server.engine.start_run("UI audit " + malicious, malicious)
    step = server.engine.record_step(run, "MEMORY_READ", "unsafe'\");window.__xss=1;//", {"value": malicious},
        lambda i, m: {"memory_updates": {malicious: malicious}, "thought": malicious}, {})
    run.status = "COMPLETED"
    server.engine.storage.save_run(run.to_dict())
    client = TestClient(server.app)

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            if not request.url.startswith("http://chronos.test/"):
                route.abort()
                return
            response = client.request(request.method, request.url.replace("http://chronos.test", ""),
                                      headers=request.headers, content=request.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

        page.route("**/*", route_request)
        page.goto("http://chronos.test/")
        page.locator("#access-token").fill("wrong-token-" * 4)
        page.locator("#auth-form button").click()
        playwright.expect(page.locator("#auth-error")).not_to_have_text("")
        assert page.locator("#auth-panel").is_visible()
        page.locator("#access-token").fill(os.environ["CHRONOS_API_TOKEN"])
        page.locator("#auth-form button").click()
        page.locator("#auth-panel").wait_for(state="hidden")
        assert page.locator("#access-token").input_value() == ""
        page.evaluate("id => fetchOverview(id)", run.run_id)
        assert malicious in page.locator("#dag-header").inner_text()
        for tab in ["memory", "raw", "timetravel"]:
            page.evaluate("tab => setInspectorTab(tab)", tab)
            assert page.locator("#inspector-content img, #inspector-content svg[onload]").count() == 0
            assert page.evaluate("window.__xss || 0") == 0
        assert page.locator("#runs-list img, #dag-header img, #steps-timeline img").count() == 0

        # The dynamic handler safely preserves quotes in tool names/run IDs.
        page.evaluate("id => selectRun(id)", run.run_id)
        assert page.evaluate("window.__xss || 0") == 0
        with page.expect_download() as download:
            page.locator("#dag-header button").click()
        result = download.value
        result.save_as(tmp_path / "export.chronos.json")
        assert Path(tmp_path / "export.chronos.json").read_text(encoding="utf-8").find(run.run_id) >= 0
        assert errors == []
        browser.close()
