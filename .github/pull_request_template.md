> **Write for the reviewer.** The description should read in a minute: what
> changes, why, and what to look at carefully. No tables, no decorative
> sections, no repeating what the diff already shows. If it does not fit in a
> few lines, the PR is too big: split it. Technical detail belongs in the
> issue or the commit message.
>
> **A diagram when it explains better than text.** For flows, execution order
> or relationships between components, a diagram replaces paragraphs: include
> it instead of describing them in prose, not in addition to.

## 📝 Description
<!-- Summary of the changes in the commit template format -->
<!-- Example: :bug: fix: read the dub country from numbered pais fields -->

**Detailed context:**
<!-- What changes and why, in a few lines. Mention linked issues (Closes #123) -->

---

## 🧪 Technical verification
- [ ] **Tests**: `python3 -m unittest test_dub_check` passes locally.
- [ ] **Verdict differential**: if parsing or matching changed, every film in `results.json` was re-evaluated against the cached wiki with the previous and the new code, and each changed verdict is listed and was checked by hand.
- [ ] **Read-only**: no POST, PUT or DELETE call to Radarr, Sonarr or the wiki was added.
- [ ] **Secrets**: no API key, `results.json`, `series-results.json` or `.cache/` is committed.
- [ ] **README**: a new verdict, mode, flag or page shape is documented.

---

## 📸 Attachments (optional)
<!-- Command output or verdict lists if relevant -->
