"""Shared audio and visual timeline transformations."""


def enrich_manifest_timeline(manifest: dict) -> dict:
    """Add absolute and chapter-relative timing to an audio manifest."""
    chapters_by_index = {
        int(chapter["index"]): chapter
        for chapter in manifest.get("chapters", [])
    }
    chapter_elapsed: dict[int, float] = {
        chapter_index: 0.0 for chapter_index in chapters_by_index
    }
    total_elapsed = 0.0

    for segment in manifest.get("segments", []):
        words = segment.get("words") or []
        inferred_duration = float(words[-1].get("end", 0.0)) if words else 0.0
        duration = max(0.001, float(segment.get("duration") or inferred_duration))
        chapter_index = int(segment["chapter_index"])
        elapsed_in_chapter = chapter_elapsed.get(chapter_index, 0.0)
        segment["duration"] = round(duration, 3)
        segment["start_time"] = round(total_elapsed, 3)
        segment["chapter_elapsed"] = round(elapsed_in_chapter, 3)
        total_elapsed += duration
        chapter_elapsed[chapter_index] = elapsed_in_chapter + duration

    chapter_start = 0.0
    for chapter in manifest.get("chapters", []):
        chapter_index = int(chapter["index"])
        duration = chapter_elapsed.get(chapter_index, 0.0)
        chapter["start_time"] = round(chapter_start, 3)
        chapter["duration"] = round(duration, 3)
        chapter["end_time"] = round(chapter_start + duration, 3)
        chapter_start += duration

    manifest["duration"] = round(total_elapsed, 3)
    return manifest


def merge_nearby_same_visual_cues(
    segments: list[dict],
    max_gap_seconds: float,
) -> None:
    """Keep the same visual on screen across short gaps between cues."""
    if max_gap_seconds < 0:
        raise ValueError("相同视觉合并间隔不能小于零")

    cue_ranges = []
    for segment in segments:
        segment_start = float(segment.get("start_time", 0))
        for cue in segment.get("cues", []):
            cue_ranges.append({
                "start": segment_start + float(cue["start"]),
                "end": segment_start + float(cue["end"]),
                "cue": cue,
                "segment": segment,
            })
    cue_ranges.sort(key=lambda item: (item["start"], item["end"]))

    groups = []
    for cue_range in cue_ranges:
        if (
            groups
            and cue_range["cue"]["visual_id"] == groups[-1]["cue"]["visual_id"]
            and cue_range["start"] - groups[-1]["end"] <= max_gap_seconds
        ):
            groups[-1]["end"] = max(groups[-1]["end"], cue_range["end"])
            groups[-1]["count"] += 1
            groups[-1]["end_quote"] = cue_range["cue"].get("end_quote", "")
        else:
            groups.append({
                **cue_range,
                "count": 1,
                "end_quote": cue_range["cue"].get("end_quote", ""),
            })

    for segment in segments:
        segment["cues"] = []

    for group in groups:
        if group["count"] == 1:
            group["segment"]["cues"].append(group["cue"])
            continue

        for segment in segments:
            segment_start = float(segment.get("start_time", 0))
            segment_end = segment_start + float(segment["duration"])
            overlap_start = max(group["start"], segment_start)
            overlap_end = min(group["end"], segment_end)
            if overlap_end <= overlap_start:
                continue

            local_start = round(overlap_start - segment_start, 3)
            local_end = round(overlap_end - segment_start, 3)
            if local_end <= local_start:
                continue

            cue = {
                **group["cue"],
                "start": local_start,
                "end": local_end,
                "end_quote": group["end_quote"],
                "merged_cue_count": group["count"],
            }
            segment["cues"].append(cue)

    for segment in segments:
        segment["cues"].sort(key=lambda cue: (cue["start"], cue["end"]))

