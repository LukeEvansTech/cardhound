from pathlib import Path

from sleeper import photos

LUST = {
    "set": "FMA",
    "who": "Lust",
    "cards": "UE09BT/FMA-1-090_p1 SR★",
    "refs": ["UE09BT_FMA-1-090_p1.jpg"],
}


def setup(tmp: Path, scores: dict, monkeypatch) -> tuple[Path, Path]:
    refs, shots = tmp / "refs", tmp / "photos"
    (refs / photos.PRINTS).mkdir(parents=True)
    shots.mkdir()
    (refs / "UE09BT_FMA-1-090_p1.jpg").write_bytes(b"x")
    for name in ("UE09BT_FMA-1-090.png", "UE09BT_FMA-1-090_p1.png", "UE09BT_FMA-1-090_p2.png"):
        (refs / photos.PRINTS / name).write_bytes(b"x")
    (shots / "1.jpg").write_bytes(b"x" * 2000)
    (refs / photos.PRINTS / "FMA-1-090.done").touch()
    monkeypatch.setattr(photos, "long_side", lambda ref: 600)
    monkeypatch.setattr(photos, "score", lambda photo, ref, side=None: scores.get(ref.stem, 0))
    return refs, shots


def test_decoys_are_the_other_prints(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    names = [p.name for p in photos.decoys_for(LUST, refs)]
    assert names == ["UE09BT_FMA-1-090.png", "UE09BT_FMA-1-090_p2.png"]


def test_plain_print_sharing_the_drawing_is_unwanted(tmp_path, monkeypatch):
    # the plain SR shares Lust's drawing with the SR★: 111 on the wanted art, 530 on the plain one
    refs, shots = setup(
        tmp_path, {"UE09BT_FMA-1-090_p1": 111, "UE09BT_FMA-1-090": 530}, monkeypatch
    )
    res = photos.check(LUST, "1", "file:", refs, shots)
    assert res["verdict"] == "unwanted"
    assert res["print"] == "UE09BT_FMA-1-090"


def test_shared_art_close_scores_stay_a_match(tmp_path, monkeypatch):
    # promos whose two parallels share art score near each other: no call either way
    refs, shots = setup(
        tmp_path, {"UE09BT_FMA-1-090_p1": 313, "UE09BT_FMA-1-090_p2": 306}, monkeypatch
    )
    res = photos.check(LUST, "1", "file:", refs, shots)
    assert res["verdict"] == "match" and "print" not in res


def test_a_listed_print_is_never_a_decoy_even_without_its_art(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    both = LUST | {"cards": "UE09BT/FMA-1-090_p1 SR★, UE09BT/FMA-1-090_p2 SR★★"}
    assert [p.name for p in photos.decoys_for(both, refs)] == ["UE09BT_FMA-1-090.png"]


def test_an_empty_card_list_page_is_tried_again(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(photos, "_get", lambda url: calls.append(url) or b"<html>busy</html>")
    tried: set = set()
    photos.fetch_prints(LUST, tmp_path, tried)
    photos.fetch_prints(LUST, tmp_path, tried)  # a sibling of the same number, same sweep
    assert len(calls) == 1
    assert not (tmp_path / photos.PRINTS / "FMA-1-090.done").exists()


def test_a_bare_number_want_takes_its_code_from_the_art(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    bare = LUST | {"cards": "090"}
    assert [p.name for p in photos.decoys_for(bare, refs)] == [
        "UE09BT_FMA-1-090.png",
        "UE09BT_FMA-1-090_p2.png",
    ]


def test_a_listed_print_without_its_own_art_counts_as_wanted(tmp_path, monkeypatch):
    # p2 is wanted too but only has card list art: its strong score is a match, not a wrong print
    scores = {"UE09BT_FMA-1-090_p1": 30, "UE09BT_FMA-1-090_p2": 400, "UE09BT_FMA-1-090": 300}
    refs, shots = setup(tmp_path, scores, monkeypatch)
    both = LUST | {"cards": "UE09BT/FMA-1-090_p1 SR★, UE09BT/FMA-1-090_p2 SR★★"}
    res = photos.check(both, "1", "file:", refs, shots)
    assert "print" not in res


def test_a_custom_named_ref_still_claims_its_catalogue_print(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    (refs / "lust-alt.jpg").write_bytes(b"x")
    custom = LUST | {"cards": "FMA-1-090_p1", "refs": ["lust-alt.jpg"]}
    assert not photos.decoys_for(custom, refs)  # its print is unknown: no call


def test_a_promo_and_a_booster_print_of_one_number_stay_apart(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    (refs / photos.PRINTS / "UEPR_FMA-1-090.png").write_bytes(b"x")
    promo = LUST | {"cards": "UEPR/FMA-1-090 C", "refs": []}
    names = [p.name for p in photos.decoys_for(promo, refs)]
    assert "UE09BT_FMA-1-090.png" in names and "UEPR_FMA-1-090.png" not in names


def test_no_call_while_a_listed_print_has_no_art(tmp_path, monkeypatch):
    scores = {"UE09BT_FMA-1-090_p1": 30, "UE09BT_FMA-1-090": 300}
    refs, shots = setup(tmp_path, scores, monkeypatch)
    (refs / photos.PRINTS / "UE09BT_FMA-1-090_p2.png").unlink()
    both = LUST | {"cards": "UE09BT/FMA-1-090_p1 SR★, UE09BT/FMA-1-090_p2 SR★★"}
    assert "print" not in photos.check(both, "1", "file:", refs, shots)


def test_a_setless_number_still_has_its_plain_print_as_a_decoy(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    numbered = LUST | {"cards": "FMA-1-090"}
    assert "UE09BT_FMA-1-090.png" in [p.name for p in photos.decoys_for(numbered, refs)]


def test_a_configured_ref_missing_from_disk_is_still_wanted(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    both = LUST | {"cards": "090", "refs": ["UE09BT_FMA-1-090_p1.jpg", "UE09BT_FMA-1-090_p2.jpg"]}
    assert [p.name for p in photos.decoys_for(both, refs)] == ["UE09BT_FMA-1-090.png"]


def test_a_bare_number_wants_every_parallel(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    bare = {"set": "FMA", "who": "Lust", "cards": "090"}
    assert [p.name for p in photos.decoys_for(bare, refs)] == ["UE09BT_FMA-1-090.png"]


def test_a_bare_number_never_borrows_another_franchise(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    (refs / photos.PRINTS / "UE15BT_EVA-1-090_p1.png").write_bytes(b"x")
    bare = {"set": "FMA", "who": "Lust", "cards": "090"}
    assert "UE15BT_EVA-1-090_p1.png" not in [p.name for p in photos.listed_prints(bare, refs, [])]


def test_no_call_until_every_print_of_the_number_is_here(tmp_path, monkeypatch):
    scores = {"UE09BT_FMA-1-090_p1": 111, "UE09BT_FMA-1-090": 530}
    refs, shots = setup(tmp_path, scores, monkeypatch)
    (refs / photos.PRINTS / "FMA-1-090.done").unlink()
    assert "print" not in photos.check(LUST, "1", "file:", refs, shots)


def test_only_a_sibling_that_wants_the_seen_print_can_take_the_listing(tmp_path, monkeypatch):
    refs, _ = setup(tmp_path, {}, monkeypatch)
    p2 = {"set": "FMA", "who": "Lust", "cards": "UE09BT/FMA-1-090_p2 SR★★"}
    assert photos.wants_print(p2, "UE09BT_FMA-1-090_p2", refs)
    assert not photos.wants_print(p2, "UE09BT_FMA-1-090", refs)

    assert not photos.wants_print(
        p2 | {"skip": ["UE09BT_FMA-1-090_p2.jpg"]}, "UE09BT_FMA-1-090_p2", refs
    )
    assert photos.wants_print(p2 | {"refs": ["lust.jpg"]}, "UE09BT_FMA-1-090", refs)
