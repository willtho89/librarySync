"""Opt-in live conformance test against disposable LibrarySync and AIOStreams servers.

Set AIOSTREAMS_TEST_AUTH_FILE to the isolated Jellyfin authentication JSON, and
LIBRARYSYNC_TEST_BASE_URL / AIOSTREAMS_TEST_BASE_URL to loopback test servers.
Never point this test at a real user's tracker configuration.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

AUTH_FILE = os.environ.get("AIOSTREAMS_TEST_AUTH_FILE")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not AUTH_FILE, reason="Disposable AIOStreams server required")]


async def test_live_jellyfin_watch_state_exchange():
    auth = json.loads(Path(AUTH_FILE).read_text())
    aio_url = os.environ["AIOSTREAMS_TEST_BASE_URL"]
    local_url = os.environ["LIBRARYSYNC_TEST_BASE_URL"]
    assert all(httpx.URL(url).host in {"127.0.0.1", "localhost"} for url in (aio_url, local_url))
    async with (
        httpx.AsyncClient(base_url=local_url, timeout=30) as local,
        httpx.AsyncClient(
            base_url=aio_url + "/jellyfin", headers={"X-Emby-Token": auth["AccessToken"]}, timeout=60
        ) as aio,
    ):
        response = await local.post(
            "/api/auth/login",
            json={
                "username": os.environ["LIBRARYSYNC_TEST_USERNAME"],
                "password": os.environ["LIBRARYSYNC_TEST_PASSWORD"],
            },
        )
        assert response.status_code == 200
        config = (await local.get("/api/stremio-addon/config")).json()
        addon_id = config["addon_id"]
        pull_path = f"/stremio-addon/{addon_id}/watch_state/pull.json"

        async def request(method, path, **kwargs):
            response = await aio.request(method, path, **kwargs)
            assert response.status_code in {200, 204}, (path, response.status_code)
            return response.json() if response.content else None

        async def wait_for(label, predicate):
            for _ in range(90):
                state = (await local.get(pull_path)).json()
                if predicate(state):
                    print(label + " verified")
                    return state
                await asyncio.sleep(1)
            events = (await local.get("/api/stremio-addon/watch-state/status")).json()["events"]
            pytest.fail(f"{label}: events {[(e['event'], e['status'], e['error']) for e in events]}")

        movie_result = await request("GET", "/Items", params={"SearchTerm": "tt0111161", "Limit": 3})
        movie = next(item for item in movie_result["Items"] if item["Name"] == "The Shawshank Redemption")
        movie_id = movie["Id"]
        # Delivered position ids are retained by AIOStreams. Use fresh positions
        # on reruns without deleting its tracker database or changing the protocol.
        offset = int(time.time() * 1000) % 100000
        pause_position = 120000 + offset
        stop_position = 240000 + offset
        await request("DELETE", f"/UserPlayedItems/{movie_id}")
        await wait_for("initial clear", lambda s: "tt0111161" not in s["watched"]["movies"])
        await request("POST", "/Sessions/Playing", json={"ItemId": movie_id, "PositionTicks": offset * 10000})
        await request(
            "POST",
            "/Sessions/Playing/Progress",
            json={
                "ItemId": movie_id,
                "PositionTicks": pause_position * 10000,
                "IsPaused": True,
            },
        )
        await wait_for(
            "pause position",
            lambda s: any(i.get("positionMs") == pause_position and not i["played"] for i in s["items"]),
        )
        await request(
            "POST",
            "/Sessions/Playing/Progress",
            json={
                "ItemId": movie_id,
                "PositionTicks": pause_position * 10000,
                "IsPaused": False,
            },
        )
        await wait_for("resume clears pause", lambda s: not any(i["metaId"] == "tt0111161" for i in s["items"]))
        await request(
            "POST", "/Sessions/Playing/Stopped", json={"ItemId": movie_id, "PositionTicks": stop_position * 10000}
        )
        await wait_for(
            "unfinished stop",
            lambda s: any(i.get("positionMs") == stop_position and not i["played"] for i in s["items"]),
        )
        assert "tt0111161" not in (await local.get(pull_path)).json()["watched"]["movies"]
        await request("POST", "/Sessions/Playing", json={"ItemId": movie_id, "PositionTicks": stop_position * 10000})
        await request(
            "POST",
            "/Sessions/Playing/Stopped",
            json={
                "ItemId": movie_id,
                "PositionTicks": int(movie["RunTimeTicks"] * 0.95) + offset * 10000,
            },
        )
        await wait_for("completed stop", lambda s: "tt0111161" in s["watched"]["movies"])
        await request("DELETE", f"/UserPlayedItems/{movie_id}")
        await wait_for("explicit unplayed", lambda s: "tt0111161" not in s["watched"]["movies"])
        await request("POST", f"/UserPlayedItems/{movie_id}")
        await wait_for("explicit played", lambda s: "tt0111161" in s["watched"]["movies"])
        await request("POST", f"/UserFavoriteItems/{movie_id}")
        await wait_for("watchlist add", lambda s: any(i["metaId"] == "tt0111161" for i in s["watchlist"]))
        await request("DELETE", f"/UserFavoriteItems/{movie_id}")
        await wait_for("watchlist remove", lambda s: not any(i["metaId"] == "tt0111161" for i in s["watchlist"]))
        await request("POST", f"/UserItems/{movie_id}/UserData", json={"Rating": 8.5})
        await wait_for(
            "decimal movie rating",
            lambda s: any(i["metaId"] == "tt0111161" and i["rating"] == 8.5 for i in s["ratings"]),
        )
        await request("DELETE", f"/UserItems/{movie_id}/Rating")
        await wait_for("rating clear", lambda s: not any(i["metaId"] == "tt0111161" for i in s["ratings"]))

        series_result = await request("GET", "/Items", params={"SearchTerm": "Breaking Bad", "Limit": 5})
        series = next(item for item in series_result["Items"] if item["Name"] == "Breaking Bad")
        series_id = series["Id"]
        await request("POST", f"/UserItems/{series_id}/Rating", params={"Likes": "false"})
        await wait_for("drop show", lambda s: "tt0903747" in s["watched"]["dropped"])
        await request("DELETE", f"/UserItems/{series_id}/Rating")
        await wait_for("undrop show", lambda s: "tt0903747" not in s["watched"]["dropped"])
        seasons = await request("GET", f"/Shows/{series_id}/Seasons")
        season = next(item for item in seasons["Items"] if item.get("IndexNumber") == 1)
        episodes = await request("GET", f"/Shows/{series_id}/Episodes", params={"Season": 1})
        assert len(episodes["Items"]) > 1
        await request("POST", f"/UserPlayedItems/{season['Id']}")
        marked = await wait_for("bulk season played", lambda s: len(s["watched"]["episodes"]) >= len(episodes["Items"]))
        assert all(i.startswith("tt0903747:") for i in marked["watched"]["episodes"])
        await request("DELETE", f"/UserPlayedItems/{season['Id']}")
        await wait_for("bulk season clear", lambda s: not s["watched"]["episodes"])

        # A local rating on another title must be imported into the real Jellyfin user data.
        other = next(item for item in movie_result["Items"] if item["Name"] == "The Godfather")
        await local.post(
            "/api/stremio-addon/watch-state/ratings/movie",
            json={
                "id": "local-pull",
                "event": "rated",
                "at": 0,
                "metaId": "tt0068646",
                "rating": 7.5,
            },
        )
        for _ in range(90):
            data = await request("GET", f"/UserItems/{other['Id']}/UserData")
            if data.get("Rating") == 7.5:
                print("LibrarySync rating imported by AIOStreams verified")
                break
            await asyncio.sleep(1)
        else:
            pytest.fail("AIOStreams did not pull LibrarySync's independent rating")


async def test_live_ratings_bulk_show_and_household_isolation():
    account_file = os.environ.get("AIOSTREAMS_TEST_ACCOUNT_FILE")
    config_file = os.environ.get("AIOSTREAMS_TEST_CONFIG_FILE")
    if not account_file or not config_file:
        pytest.skip("Disposable AIOStreams configuration files required")
    auth = json.loads(Path(AUTH_FILE).read_text())
    account = json.loads(Path(account_file).read_text())
    config = json.loads(Path(config_file).read_text())
    aio_url = os.environ["AIOSTREAMS_TEST_BASE_URL"]
    local_url = os.environ["LIBRARYSYNC_TEST_BASE_URL"]
    assert all(httpx.URL(url).host in {"127.0.0.1", "localhost"} for url in (aio_url, local_url))
    async with (
        httpx.AsyncClient(base_url=local_url, timeout=30) as owner,
        httpx.AsyncClient(base_url=local_url, timeout=30) as viewer,
        httpx.AsyncClient(base_url=aio_url, timeout=60) as aio,
    ):
        response = await owner.post(
            "/api/auth/login",
            json={
                "username": os.environ["LIBRARYSYNC_TEST_USERNAME"],
                "password": os.environ["LIBRARYSYNC_TEST_PASSWORD"],
            },
        )
        assert response.status_code == 200
        addon_id = (await owner.get("/api/stremio-addon/config")).json()["addon_id"]
        pull_path = f"/stremio-addon/{addon_id}/watch_state/pull.json"
        password = "household-disposable-password"
        await viewer.post("/api/auth/register", json={"username": "watch-state-household-test", "password": password})
        assert (
            await viewer.post(
                "/api/auth/login",
                json={
                    "username": "watch-state-household-test",
                    "password": password,
                },
            )
        ).status_code == 200
        bindings = (await owner.get("/api/stremio-addon/watch-state/status")).json()["viewers"]
        for binding in bindings:
            if binding["viewer"] == "sam":
                await owner.delete(f"/api/stremio-addon/watch-state/viewers/{binding['id']}")
        invitation = (await owner.post("/api/stremio-addon/watch-state/viewers", json={"viewer": "sam"})).json()
        assert (
            await viewer.post(
                "/api/stremio-addon/watch-state/viewers/accept",
                json={
                    "invitation": invitation["invitation"],
                },
            )
        ).status_code == 200
        config["jellyfin"]["personas"] = [
            {
                "id": "sam",
                "name": "Sam",
                "history": "own",
                "trackers": ["local-librarysync"],
            }
        ]
        response = await aio.put(
            "/api/v1/user", auth=(account["uuid"], os.environ["AIOSTREAMS_TEST_PASSWORD"]), json={"config": config}
        )
        assert response.status_code == 200
        result = await aio.post(
            "/jellyfin/Users/AuthenticateByName",
            json={
                "Username": account["uuid"] + "/Sam",
                "Pw": os.environ["AIOSTREAMS_TEST_PASSWORD"],
            },
        )
        assert result.status_code == 200
        viewer_token = result.json()["AccessToken"]
        primary_headers = {"X-Emby-Token": auth["AccessToken"]}

        async def request(method, path, **kwargs):
            response = await aio.request(method, "/jellyfin" + path, headers=primary_headers, **kwargs)
            assert response.status_code in {200, 204}, (path, response.status_code)
            return response.json() if response.content else None

        async def wait_for(label, predicate, *, household=False):
            for _ in range(90):
                state = (await owner.get(pull_path, params={"viewer": "sam"} if household else {})).json()
                if predicate(state):
                    print(label + " verified")
                    return state
                await asyncio.sleep(1)
            pytest.fail(label + " not reflected by LibrarySync")

        series_result = await request("GET", "/Items", params={"SearchTerm": "Breaking Bad", "Limit": 5})
        series = next(item for item in series_result["Items"] if item["Name"] == "Breaking Bad")
        seasons = await request("GET", f"/Shows/{series['Id']}/Seasons")
        season = next(item for item in seasons["Items"] if item.get("IndexNumber") == 1)
        episodes = await request("GET", f"/Shows/{series['Id']}/Episodes", params={"Season": 1})
        episode = episodes["Items"][0]
        for item, score in [(series, 6.25), (season, 7.25), (episode, 8.25)]:
            await request("POST", f"/UserItems/{item['Id']}/UserData", json={"Rating": score})
        state = await wait_for(
            "independent show, season and episode ratings",
            lambda s: {6.25, 7.25, 8.25}.issubset({r["rating"] for r in s["ratings"]}),
        )
        assert not state["watched"]["episodes"]
        rows = [r for r in state["ratings"] if r["metaId"] == "tt0903747"]
        assert any(r.get("season") == 1 and not r.get("videoId") for r in rows)
        assert any(r.get("videoId") for r in rows)
        assert any("season" not in r and "videoId" not in r for r in rows)
        await request("POST", f"/UserPlayedItems/{series['Id']}")
        await wait_for("bulk show played", lambda s: len(s["watched"]["episodes"]) >= 62)
        await request("DELETE", f"/UserPlayedItems/{series['Id']}")
        await wait_for("bulk show clear", lambda s: not s["watched"]["episodes"])

        results = await request("GET", "/Items", params={"SearchTerm": "tt0068646", "Limit": 3})
        movie = next(i for i in results["Items"] if i["Name"] == "The Godfather")
        response = await aio.post(f"/jellyfin/UserPlayedItems/{movie['Id']}", headers={"X-Emby-Token": viewer_token})
        assert response.status_code == 200
        await wait_for("household viewer watch", lambda s: "tt0068646" in s["watched"]["movies"], household=True)
        owner_state = (await owner.get(pull_path)).json()
        assert "tt0068646" not in owner_state["watched"]["movies"]
        bindings = (await viewer.get("/api/stremio-addon/watch-state/status")).json()["viewers"]
        binding = next(b for b in bindings if b["viewer"] == "sam")
        assert (await viewer.delete(f"/api/stremio-addon/watch-state/viewers/{binding['id']}")).status_code == 204
        assert (await owner.get(pull_path, params={"viewer": "sam"})).status_code == 404
        print("household history isolation and consent revocation verified")
