"""Tests for the JSON-backed SearchStore: saved-search CRUD, favorites,
persisted-listing diffing, and 'my location'. All offline, one tmp dir each."""

from app.models import SearchFilters
from app.store import DEFAULT_SEARCHES, SearchStore


def _store(tmp_path) -> SearchStore:
    return SearchStore(str(tmp_path))


def test_seeds_defaults_on_first_run(tmp_path):
    st = _store(tmp_path)
    assert st.list() == DEFAULT_SEARCHES == []
    assert (tmp_path / "searches.json").exists()


def test_data_dir_falls_back_when_unwritable(tmp_path, monkeypatch):
    # A file can't be a parent dir, so mkdir under it raises OSError; the store
    # must fall back to ./data rather than crashing at startup.
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    st = SearchStore(str(blocker / "nested"))
    st.add("Watched car", SearchFilters(make="Volkswagen"))
    assert (tmp_path / "data" / "searches.json").exists()
    assert [s.name for s in st.list()] == ["Watched car"]


def test_add_get_update_delete(tmp_path):
    st = _store(tmp_path)
    added = st.add("My VW Search", SearchFilters(make="Volkswagen", model="Golf Alltrack"))
    assert added.id == "my-vw-search"                 # name slugified
    assert st.get("my-vw-search").name == "My VW Search"

    updated = st.update("my-vw-search", "Renamed", SearchFilters(make="Audi"))
    assert updated.name == "Renamed"
    assert updated.filters.make == "Audi"
    assert st.update("nope", "x", SearchFilters()) is None

    assert st.delete("my-vw-search") is True
    assert st.get("my-vw-search") is None
    assert st.delete("my-vw-search") is False         # already gone


def test_add_slug_collision_gets_unique_suffix(tmp_path):
    st = _store(tmp_path)
    a = st.add("Same Name", SearchFilters())
    b = st.add("Same Name", SearchFilters())
    assert a.id == "same-name"
    assert a.id != b.id and b.id.startswith("same-name-")


def test_add_survives_reload(tmp_path):
    _store(tmp_path).add("Persisted", SearchFilters(make="Mazda"))
    reloaded = _store(tmp_path)                        # new instance, same dir
    assert reloaded.get("persisted").filters.make == "Mazda"


def test_location_get_set(tmp_path):
    st = _store(tmp_path)
    assert st.get_location() == {"zip": "", "radius": 100}   # default
    assert st.set_location("80022", 150) == {"zip": "80022", "radius": 150}
    assert st.get_location() == {"zip": "80022", "radius": 150}
    assert st.set_location("  ", 0) == {"zip": "", "radius": 0}  # trimmed + zeroed


def test_save_listings_add_and_grace_diff(tmp_path):
    st = _store(tmp_path)
    sid = "vw-alltrack-2019"
    r1 = st.save_listings(sid, [{"id": "a"}, {"id": "b"}])
    assert set(r1["added"]) == {"a", "b"} and r1["removed"] == []
    assert all(l["first_seen"] and l["last_seen"] and not l["stale"] for l in r1["listings"])

    # b disappears: kept for the grace window flagged stale, NOT removed.
    r2 = st.save_listings(sid, [{"id": "a"}, {"id": "c"}])
    assert r2["added"] == ["c"]
    assert r2["removed"] == []
    by_id = {l["id"]: l for l in st.get_listings(sid)}
    assert set(by_id) == {"a", "b", "c"}
    assert by_id["b"]["stale"] and not by_id["a"]["stale"]

    # b comes back within grace: no re-"added" (no duplicate notification).
    r3 = st.save_listings(sid, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    assert r3["added"] == [] and r3["removed"] == []
    assert not {l["id"]: l for l in st.get_listings(sid)}["b"]["stale"]


def test_save_listings_purges_after_grace(tmp_path):
    st = _store(tmp_path)
    sid = "x"
    st.save_listings(sid, [{"id": "a"}, {"id": "b"}])
    # Negative grace → anything missing from this run is past grace: purged.
    # (0 would be timing-dependent when two runs land on the same clock tick.)
    r = st.save_listings(sid, [{"id": "a"}], grace_seconds=-1)
    assert r["removed"] == ["b"]
    assert {l["id"] for l in st.get_listings(sid)} == {"a"}


def test_capped_run_keeps_missing_without_staling(tmp_path):
    # complete=False (run hit its limit): a listing missing from this partial view
    # is kept as-is — not flagged stale, not removed, not re-notified when it returns.
    st = _store(tmp_path)
    sid = "x"
    st.save_listings(sid, [{"id": "a"}, {"id": "b"}])
    r = st.save_listings(sid, [{"id": "a"}], complete=False)
    assert r["removed"] == []
    by = {l["id"]: l for l in r["listings"]}
    assert set(by) == {"a", "b"}
    assert by["b"].get("stale") is not True     # not falsely "possibly sold"
    # returning next run is not counted as new (it never left the stored set)
    r2 = st.save_listings(sid, [{"id": "a"}, {"id": "b"}], complete=False)
    assert r2["added"] == []


def test_complete_run_stales_missing(tmp_path):
    st = _store(tmp_path)
    sid = "x"
    st.save_listings(sid, [{"id": "a"}, {"id": "b"}])
    r = st.save_listings(sid, [{"id": "a"}], complete=True)   # full view: b truly gone
    assert next(l for l in r["listings"] if l["id"] == "b")["stale"] is True


def test_save_listings_preserves_first_seen(tmp_path):
    st = _store(tmp_path)
    sid = "x"
    st.save_listings(sid, [{"id": "a"}])
    stamped = st.get_listings(sid)[0]["first_seen"]
    assert isinstance(stamped, float)                      # store stamps it
    st.save_listings(sid, [{"id": "a", "first_seen": 1.0}])  # re-seen; caller value ignored
    assert st.get_listings(sid)[0]["first_seen"] == stamped  # original kept


def test_delete_search_clears_its_listings(tmp_path):
    st = _store(tmp_path)
    st.add("Temp", SearchFilters())
    st.save_listings("temp", [{"id": "a"}])
    assert [l["id"] for l in st.get_listings("temp")] == ["a"]
    st.delete("temp")
    assert st.get_listings("temp") == []


def test_favorites_add_remove_dedup(tmp_path):
    st = _store(tmp_path)
    assert st.list_favorites() == []
    fav = {"id": "f1", "title": "Car 1", "site": "cars_com", "url": "https://x/1"}
    st.add_favorite(fav)
    st.add_favorite(fav)                               # duplicate id ignored
    assert [f["id"] for f in st.list_favorites()] == ["f1"]

    st.add_favorite({"id": "f2", "title": "Car 2", "site": "demo", "url": "https://x/2"})
    assert {f["id"] for f in st.list_favorites()} == {"f1", "f2"}

    st.remove_favorite("f1")
    assert [f["id"] for f in st.list_favorites()] == ["f2"]
    st.remove_favorite("missing")                      # no-op, no error
    assert [f["id"] for f in st.list_favorites()] == ["f2"]
