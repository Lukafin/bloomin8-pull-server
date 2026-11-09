# Daily e-ink archive refresh (Luka-authorized)

Run this stage once on **every** daily infographic job execution, after image publication/verification (if any) and **before** the longer podcast stage and final response. If there is no eligible work, a research-only assignment, private/pending-review image, or image/podcast failure, still attempt this independent refresh from the already **public** archive. It must not create/claim/publish another Roj task.

## Commands

```bash
/usr/bin/python3 /home/lukafinzgar/projects/bloomin8-pull-server/refresh_archive.py --playlist /home/lukafinzgar/.hermes/local/commute-frame/reviewed-archive/playlist.json candidates --limit 3
```

- `already_refreshed_today`: report that briefly; no more admission today.
- Otherwise inspect candidates newest first. The helper reads the public `entries.json`, filters Slovenian `age_6` entries newer than the original frozen-list cutoff, skips admitted/rejected images, downloads the actual originals into a private staging directory, and checks portrait dimensions. These mechanical checks **are not visual approval**.
- Load the actual image at the returned `path` with `vision_analyze`. Check actual Slovenian text, concrete educational content suitable for Luka's 7/9-year-old children, legible main copy and a portrait composition useful on a 1200×1600 e-ink screen. Small source-footnote text is acceptable; tiny main explanatory text, visibly wrong language, dense/abstract unsuitable material or unreadable color-dependent content is not. Metadata alone is never enough. Do not claim physical display verification.
- Reject unsuitable candidates (with the actual visual reason), then inspect the next candidate. Admit **at most one** image, preferring the newest suitable candidate. Do not regenerate, translate, edit or add overlays to rejected images.

```bash
/usr/bin/python3 /home/lukafinzgar/projects/bloomin8-pull-server/refresh_archive.py --playlist /home/lukafinzgar/.hermes/local/commute-frame/reviewed-archive/playlist.json approve EXACT_FILENAME --review-note 'Specific findings from the actual visual review'
# Or persist a visual rejection:
/usr/bin/python3 /home/lukafinzgar/projects/bloomin8-pull-server/refresh_archive.py --playlist /home/lukafinzgar/.hermes/local/commute-frame/reviewed-archive/playlist.json reject EXACT_FILENAME --review-note 'Specific reason the actual image failed visual review'
```

Pass filenames exactly as returned, with safe argument quoting. The note is an audit record of your real review, not an instruction to pretend you reviewed it.

## Verification and boundaries

- Approval rechecks public publication/identity, downloaded/public hashes, dimensions and age/language metadata; it backs up and atomically replaces only the private playlist after storing verified original bytes.
- Read back the exact playlist using `archive_rotation.load_selection`: today's choice must be unchanged; tomorrow's choice must be the admitted filename. Verify the cached original hash matches `refresh.reviews[filename].sha256`.
- The new image is first **the following Ljubljana morning at 08:00**. Existing reviewed images remain in rotation as fallback on days without another suitable admission. The server reloads this file on each request: no restart, config edit, wake change, device_id probe, manual push or device wake is needed or authorized by this stage.
- If no suitable new image exists or sources fail, keep the existing playlist unchanged and report the exact limitation. Never use unpublished/private/admin-pending images or an unchecked `latest.json` fallback. Do not block/undo/repeat an already successful image or podcast operation because refresh failed.
- Include in the daily final report: admitted filename/subject and next display date, or no-admission reason; actual visual review result; today's-choice preservation and cache-hash read-back result. Distinguish scheduled server selection from physical-panel acceptance.
