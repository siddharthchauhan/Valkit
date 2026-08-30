"""Static checks on the web console.

These are cheap, and they guard claims the console makes about itself that no
runtime test would catch — and that a reviewer would otherwise have to take on
trust.

The credential-containment claim is the important one. The console asserts that
`sign.js` is the only file that reads a signature component; that is what makes
containment a review of one file rather than of a whole front end. A test is the
only thing that keeps it true after the next change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "api" / "static"
SIGN = STATIC / "sign.js"


def js_files() -> list[Path]:
    return sorted(p for p in STATIC.rglob("*.js"))


def code(path: Path) -> str:
    """Source with comments removed.

    These tests check what the console *does*. Its comments explain what it
    deliberately does not do — "never innerHTML", "no console.* call" — and a
    check that matched its own explanation would fail for saying the right
    thing.
    """
    source = path.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
    return source


def other_js() -> list[Path]:
    return [p for p in js_files() if p != SIGN]


class TestCredentialContainment:
    def test_only_sign_js_reads_a_credential(self):
        """The single-file rule, enforced.

        `copy.js` holds the labels a person reads — "Password (signature
        component)" — and never a value, so it is exempt from the value rule
        but not from the URL rule below.
        """
        offenders = []
        for path in other_js():
            if path.name == "copy.js":
                continue
            source = code(path)
            for pattern in (r"\bpassword\s*:", r"\.value\b.*password", r"components\s*:"):
                if re.search(pattern, source):
                    offenders.append(f"{path.name}: matches {pattern}")
        assert offenders == [], offenders

    def test_sign_js_makes_no_console_call(self):
        """A console.* call in the file holding the credential is one accident
        away from printing it."""
        assert not re.search(r"\bconsole\s*\.", code(SIGN))

    def test_no_credential_reaches_a_url(self):
        source = SIGN.read_text()
        # The value goes in the body of a POST and nowhere else.
        assert "JSON.stringify" in source
        assert not re.search(r"[?&](password|components|credential)=", source)
        for path in js_files():
            assert not re.search(
                r"(searchParams|URLSearchParams)[^\n]*password", code(path)
            ), path.name

    def test_no_credential_reaches_storage(self):
        for path in js_files():
            for line in code(path).splitlines():
                if "Storage" in line and re.search(r"password|credential", line):
                    pytest.fail(f"{path.name}: {line.strip()}")

    def test_the_credential_field_cannot_be_serialised_by_a_form(self):
        """No `name` on the input and no `action` on the form: a stray form
        submission cannot put the value in a query string."""
        sign_view = (STATIC / "views" / "sign.js").read_text()
        assert "id: 'sign-pw'" in sign_view
        assert re.search(r"name:\s*'sign-pw'", sign_view) is None
        assert "action" not in re.search(
            r"el\('form', \{ id: 'sign-form'.*?\}", sign_view, re.S
        ).group()

    def test_autocomplete_is_off_not_current_password(self):
        """A component a browser can store, sync and replay without the
        individual weakens the basis of 11.200(a)(1)(i)."""
        sign_view = (STATIC / "views" / "sign.js").read_text()
        assert "current-password" not in sign_view
        assert sign_view.count("autocomplete: 'off'") >= 2


class TestNoExternalRequests:
    """The console must work in an air-gapped single-tenant VPC."""

    def test_no_remote_resource_is_referenced(self):
        pattern = re.compile(r"""https?://(?!127\.0\.0\.1|localhost)""")
        for path in [*js_files(), STATIC / "index.html", STATIC / "styles.css"]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                # A regulatory citation in prose is not a resource.
                if pattern.search(line) and "href=" in line.lower():
                    pytest.fail(f"{path.name}:{number} {line.strip()}")

    def test_no_webfont_is_loaded(self):
        css = (STATIC / "styles.css").read_text()
        assert "@font-face" not in css
        assert "fonts.googleapis" not in css
        assert "@import" not in css

    def test_the_html_loads_exactly_one_script_and_one_stylesheet(self):
        html = (STATIC / "index.html").read_text()
        assert html.count("<script") == 1
        assert 'src="/static/app.js"' in html
        assert html.count("<link rel=\"stylesheet\"") == 1


class TestAppendOnlyLanguage:
    """Nothing in the interface may imply a record can be changed."""

    FORBIDDEN = {"delete", "archive", "revoke", "undo", "retract", "discard",
                 "unsign", "edit", "save", "reset"}

    def test_no_control_offers_to_mutate_a_record(self):
        """Checks the labels of controls, not prose. The standing disclaimer
        says "There is no PUT, PATCH or DELETE", which is the rule being
        stated rather than broken."""
        offenders = []
        for path in js_files():
            for match in re.finditer(r"text:\s*'([^']+)'", code(path)):
                label = match.group(1).strip().lower().rstrip('.')
                if label in self.FORBIDDEN or label.split()[0] in self.FORBIDDEN:
                    offenders.append(f"{path.name}: {match.group(1)}")
        assert offenders == [], offenders

    def test_the_console_never_issues_a_mutating_request(self):
        for path in js_files():
            source = code(path)
            for verb in ("'PUT'", "'PATCH'", "'DELETE'", '"PUT"', '"PATCH"', '"DELETE"'):
                assert verb not in source, f"{path.name} references {verb}"

    def test_compliant_only_ever_appears_in_the_negative(self):
        """The word is not banned — the disclaimer needs it — but every use of
        it must deny the claim rather than make it."""
        copy = (STATIC / "copy.js").read_text()
        sentences = re.findall(r"[^.!?]*\bcompliant\b[^.!?]*", copy)
        assert sentences
        for sentence in sentences:
            assert re.search(r"\bnot\b|\bdoes not\b", sentence), sentence


class TestStatisticalHonesty:
    def test_no_percentage_is_ever_rendered(self):
        """A percentage invites a reader to compare an observed rate against a
        target; the comparison that matters is the lower bound against it."""
        fmt = (STATIC / "fmt.js").read_text()
        assert "toFixed(4)" in fmt
        assert "* 100" not in fmt
        for path in js_files():
            assert "%'" not in code(path).replace("95%", ""), path.name

    def test_no_symmetric_notation_appears(self):
        """The bound is one-sided. A ± or an interval bracket would draw an
        upper limit that was never computed."""
        for path in js_files():
            assert "±" not in code(path), path.name
        assert "±" not in (STATIC / "styles.css").read_text()

    def test_the_lower_bound_is_the_largest_type_in_the_console(self):
        css = (STATIC / "styles.css").read_text()
        bound = re.search(r"\.claim \.bound \{[^}]*?(\d+)px", css)
        assert bound, "the bound has no explicit size"
        largest = max(int(n) for n in re.findall(r"font(?:-size)?:[^;]*?(\d{2})px", css))
        assert int(bound.group(1)) == largest


class TestTheming:
    def test_every_colour_token_exists_in_both_themes(self):
        """A token defined only inside a media block leaves a page borrowing
        the host's theme for that one value."""
        css = (STATIC / "styles.css").read_text()
        blocks = re.findall(r":root(?:\[data-theme=\"dark\"\])?[^{]*\{(.*?)\n\}", css, re.S)
        assert len(blocks) >= 2

        def tokens(block):
            return {m for m in re.findall(r"(--[a-z0-9-]+):", block)
                    if not m.startswith(("--sans", "--serif", "--mono"))}

        light = tokens(blocks[0])
        for block in blocks[1:]:
            dark = tokens(block)
            if not dark:
                continue
            assert light <= dark or dark <= light, sorted(light ^ dark)

    def test_the_explicit_toggle_wins_in_both_directions(self):
        css = (STATIC / "styles.css").read_text()
        assert ':root:not([data-theme="light"])' in css
        assert ':root[data-theme="dark"]' in css

    def test_the_body_paints_its_own_background(self):
        css = (STATIC / "styles.css").read_text()
        assert re.search(r"body \{[^}]*background: var\(--ground\)", css)


class TestPrint:
    def test_the_digest_survives_the_button_hide(self):
        """The digest's characters live inside the mark button, so a blanket
        `button { display: none }` in print would blank every digest — and an
        unresolvable printout defeats the digest register."""
        css = (STATIC / "styles.css").read_text()
        print_block = css[css.index("@media print"):]
        assert "button:not(.digest-mark)" in print_block
        assert re.search(r"\.digest-mark \{[^}]*display: inline !important", print_block)

    def test_the_full_digest_is_shown_on_paper(self):
        css = (STATIC / "styles.css").read_text()
        print_block = css[css.index("@media print"):]
        assert ".digest .short { display: none; }" in print_block
        assert ".digest .full { display: inline;" in print_block

    def test_wide_tables_do_not_clip(self):
        css = (STATIC / "styles.css").read_text()
        print_block = css[css.index("@media print"):]
        assert "overflow: visible !important" in print_block

    def test_the_disclaimer_is_never_hidden(self):
        css = (STATIC / "styles.css").read_text()
        print_block = css[css.index("@media print"):]
        assert ".standing { display: block !important" in print_block


class TestAccessibility:
    def test_the_page_has_a_skip_link_and_a_live_region(self):
        html = (STATIC / "index.html").read_text()
        assert 'class="skip"' in html
        assert 'role="status"' in html and 'aria-live="polite"' in html

    def test_every_landmark_is_labelled(self):
        html = (STATIC / "index.html").read_text()
        for landmark in ('role="banner"', 'role="contentinfo"', 'aria-label="Console"'):
            assert landmark in html, landmark

    def test_state_tokens_carry_a_text_expansion(self):
        """Colour is never a carrier, and neither is a three-letter code on its
        own: each token ships a visually hidden expansion."""
        dom = (STATIC / "dom.js").read_text()
        assert "TOKEN_MEANING" in dom
        assert "class: 'vh'" in dom

    def test_no_pictographic_glyph_is_used_for_state(self):
        for path in js_files():
            source = path.read_text()
            for glyph in ("✓", "✗", "✔", "✘", "⚠", "❌", "✅"):
                assert glyph not in source, f"{path.name} uses {glyph}"


class TestServerProse:
    def test_prose_is_never_injected_as_markup(self):
        """A specification, a requirement or an error body reaching the page
        through innerHTML would be an injection route."""
        for path in js_files():
            assert "innerHTML" not in code(path), path.name

    def test_the_two_typefaces_are_declared_and_distinct(self):
        css = (STATIC / "styles.css").read_text()
        assert re.search(r"\.server \{[^}]*font-family: var\(--serif\)", css)
        assert "--serif:" in css and "--sans:" in css
