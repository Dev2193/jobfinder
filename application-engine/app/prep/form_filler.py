"""§7 Stage 5 — Prepare to submit: fills a Greenhouse application form and
stops at the submit button. This module never clicks submit under any
circumstances — that is a separate, deliberate action reserved for the
approval UI, resumed only at the candidate's own explicit initiative.

Per §1's non-negotiable rules: never solve CAPTCHAs or defeat bot detection
(rule 3), never enter credentials/payment/government-ID data (rule 2), and
anything unmapped or blocked escalates to NEEDS_HUMAN rather than guessing.
"""

import re
from pathlib import Path

from app.answers_bank import AnswersBank
from app.models import MasterResume

BOT_WALL_TITLE_PATTERNS = [
    "just a moment", "attention required", "access denied",
    "are you a human", "verify you are human", "checking your browser",
]
CAPTCHA_IFRAME_SRC_PATTERNS = ["challenges.cloudflare.com", "recaptcha/api2", "hcaptcha.com"]

DECLINE_OPTION_SUBSTRINGS = ["decline", "prefer not", "don't wish", "do not wish", "not disclosed", "choose not"]

# Standard Greenhouse field IDs, consistent across the standard job-boards.greenhouse.io
# template (confirmed against a live posting). Not guaranteed on every custom-branded
# deployment — a missing element is simply skipped, not an error.
STANDARD_TEXT_FIELDS = {
    "first_name": lambda resume, ab: (resume.identity.name.split()[0] if resume.identity.name else ""),
    "last_name": lambda resume, ab: (
        " ".join(resume.identity.name.split()[1:]) if resume.identity.name and len(resume.identity.name.split()) > 1 else ""
    ),
    "email": lambda resume, ab: resume.identity.email,
    "phone": lambda resume, ab: resume.identity.phone,
}

# (pattern, resolver) — first match wins. resolver(resume, answers_bank) returns:
#   {"type": "text", "value": str}
#   {"type": "boolean", "value": bool | None}   # None means "we don't know" -> unmapped
#   {"type": "decline_option"}                   # match any decline/prefer-not-to-say option
#   None                                          # no rule matches at all -> unmapped
_QUESTION_RESOLVERS: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"linkedin", re.I), lambda r, ab: {"type": "text", "value": r.identity.linkedin or ab.linkedin_url}),
    (re.compile(r"sponsorship", re.I), lambda r, ab: {"type": "boolean", "value": ab.requires_sponsorship}),
    (re.compile(r"(legally |lawfully )?authorized to work|eligible to work", re.I),
     lambda r, ab: {"type": "boolean", "value": ab.work_authorized}),
    (re.compile(r"previously (worked|consulted)|worked (here|(at|for) .*)\s*before", re.I),
     lambda r, ab: {"type": "boolean", "value": ab.previously_employed_here}),
    (re.compile(r"non-?compete|post-employment restriction|employment agreement", re.I),
     lambda r, ab: {"type": "boolean", "value": ab.has_non_compete}),
    (re.compile(r"willing to relocate|relocation", re.I), lambda r, ab: {"type": "boolean", "value": ab.willing_to_relocate}),
    (re.compile(r"\bremote\b", re.I), lambda r, ab: {"type": "boolean", "value": ab.open_to_remote}),
    (re.compile(r"\bhybrid\b", re.I), lambda r, ab: {"type": "boolean", "value": ab.open_to_hybrid}),
    (re.compile(r"notice period", re.I), lambda r, ab: {"type": "text", "value": ab.notice_period}),
    (re.compile(r"earliest.*start|when.*(available|able) to (start|join)|start date", re.I),
     lambda r, ab: {"type": "text", "value": ab.earliest_start_date}),
    (re.compile(r"desired salary|salary expectation|compensation expectation", re.I),
     lambda r, ab: {"type": "text", "value": ab.desired_salary}),
    (re.compile(r"referral|how did you hear", re.I), lambda r, ab: {"type": "text", "value": ab.referral_source}),
    (re.compile(r"expected graduation|graduation date", re.I),
     lambda r, ab: {"type": "text", "value": r.education[0].end if r.education else ""}),
    (re.compile(r"current (country of )?residence|where are you (currently )?(based|located)|current location", re.I),
     lambda r, ab: {"type": "text", "value": r.identity.location}),
    (re.compile(r"\bgender\b", re.I), lambda r, ab: {"type": "decline_option"}),
    (re.compile(r"hispanic|latino", re.I), lambda r, ab: {"type": "decline_option"}),
    (re.compile(r"veteran", re.I), lambda r, ab: {"type": "decline_option"}),
    (re.compile(r"disability", re.I), lambda r, ab: {"type": "decline_option"}),
    (re.compile(r"accommodat", re.I), lambda r, ab: {"type": "text", "value": ""}),
]


def detect_bot_wall(page) -> str | None:
    """Check for a bot-detection wall (Cloudflare challenge, reCAPTCHA/hCaptcha
    widget). Never interact with anything found here — just detect and report."""
    title = (page.title() or "").lower()
    for pattern in BOT_WALL_TITLE_PATTERNS:
        if pattern in title:
            return f"Bot-detection challenge page (title: {page.title()!r})"
    for src_pattern in CAPTCHA_IFRAME_SRC_PATTERNS:
        try:
            iframe = page.query_selector(f'iframe[src*="{src_pattern}"]')
        except Exception:
            iframe = None
        if iframe is not None and iframe.is_visible():
            return f"Visible CAPTCHA widget detected ({src_pattern})"
    return None


def _resolve_question(label: str, resume: MasterResume, answers_bank: AnswersBank) -> dict | None:
    for pattern, resolver in _QUESTION_RESOLVERS:
        if pattern.search(label):
            return resolver(resume, answers_bank)
    return None


def _select_react_option(page, input_locator, desired_bool: bool | None) -> tuple[bool, str]:
    """Click a Greenhouse react-select combobox and pick the option matching
    desired_bool. Returns (success, reason). Only clicks when there is a
    single, unambiguous matching option — e.g. a plain "No" — never guesses
    among several specific variants (e.g. multiple "Yes, <visa type>" options),
    since picking the wrong one would misrepresent the candidate."""
    if desired_bool is None:
        return False, "no answer available for this field"

    try:
        input_locator.click()
        page.wait_for_selector('div[class*="select__option"]', timeout=3000, state="visible")
    except Exception:
        return False, "dropdown did not open"

    options = page.locator('div[class*="select__option"]')
    texts = [t.strip() for t in options.all_inner_texts()]

    wanted_prefix = "yes" if desired_bool else "no"
    exact_matches = [t for t in texts if t.lower() == wanted_prefix]
    prefix_matches = [t for t in texts if t.lower().startswith(wanted_prefix)]

    target_text = None
    if exact_matches:
        target_text = exact_matches[0]
    elif len(prefix_matches) == 1:
        target_text = prefix_matches[0]

    if target_text is None:
        page.keyboard.press("Escape")
        return False, f"ambiguous options for this field: {texts}"

    options.filter(has_text=target_text).first.click()
    return True, ""


def _select_decline_option(page, input_locator) -> tuple[bool, str]:
    try:
        input_locator.click()
        page.wait_for_selector('div[class*="select__option"]', timeout=3000, state="visible")
    except Exception:
        return False, "dropdown did not open"

    options = page.locator('div[class*="select__option"]')
    texts = [t.strip() for t in options.all_inner_texts()]
    for i, text in enumerate(texts):
        if any(sub in text.lower() for sub in DECLINE_OPTION_SUBSTRINGS):
            options.nth(i).click()
            return True, ""

    page.keyboard.press("Escape")
    return False, f"no decline-to-answer option found among: {texts}"


def _has_application_form(page) -> bool:
    """True if the page has at least one recognizable Greenhouse
    application-form element. Distinguishes "found the form and it simply
    has zero custom questions" from "landed on the wrong page entirely" —
    some ATS-embedded custom domains (e.g. a company's own /jobs/search
    page with a ?gh_jid= query param) don't reliably route straight to the
    specific job's apply form on a plain page load, and silently reporting
    zero filled/zero missing in that case would make an empty search page
    look identical to a genuinely simple form."""
    for field_id in (*STANDARD_TEXT_FIELDS.keys(), "resume"):
        if page.locator(f"#{field_id}").count() > 0:
            return True
    return page.locator('input[id^="question_"]').count() > 0


def _fill_greenhouse_application(page, resume: MasterResume, answers_bank: AnswersBank, docx_path) -> dict:
    """The classic job-boards.greenhouse.io embed — confirmed working
    against a live posting. Not every Greenhouse-branded posting uses this
    template (some, like Stripe's, are a fully custom-built application
    page with no recognizable fields at all — those correctly fall through
    to no_form_detected rather than being guessed at)."""
    if not _has_application_form(page):
        return {"filled": [], "unmapped_required": [], "skipped_optional": [], "no_form_detected": True}

    filled, unmapped_required, skipped_optional = [], [], []

    for field_id, resolver in STANDARD_TEXT_FIELDS.items():
        locator = page.locator(f"#{field_id}")
        if locator.count() == 0:
            continue
        value = resolver(resume, answers_bank)
        if value:
            locator.fill(value)
            filled.append(field_id)

    resume_input = page.locator("#resume")
    if resume_input.count() > 0:
        if docx_path is not None and Path(docx_path).exists():
            resume_input.set_input_files(str(docx_path))
            filled.append("resume")
        else:
            # A resume upload field exists on this form but there's no resume
            # file to attach (none on record, or the recorded path no longer
            # exists on disk). Never submit an application with no resume —
            # this must escalate, not silently proceed with a blank field.
            unmapped_required.append("resume (no resume file on file — upload one via the intake page)")

    # Custom per-posting questions: each one's label is in a <label> tied to
    # the input's id, mirroring the structure confirmed on a live posting.
    question_inputs = page.locator('input[id^="question_"]')
    for i in range(question_inputs.count()):
        el = question_inputs.nth(i)
        field_id = el.get_attribute("id")
        if not field_id:
            continue
        label_el = page.locator(f'label[for="{field_id}"]')
        label_text = label_el.inner_text().strip() if label_el.count() > 0 else ""
        is_required = "*" in label_text or (el.get_attribute("aria-required") == "true")

        resolution = _resolve_question(label_text, resume, answers_bank)
        if resolution is None:
            (unmapped_required if is_required else skipped_optional).append(label_text or field_id)
            continue

        if resolution["type"] == "text":
            if resolution["value"]:
                el.fill(resolution["value"])
                filled.append(label_text or field_id)
            elif is_required:
                unmapped_required.append(label_text or field_id)
            else:
                skipped_optional.append(label_text or field_id)
        elif resolution["type"] == "boolean":
            ok, reason = _select_react_option(page, el, resolution["value"])
            if ok:
                filled.append(label_text or field_id)
            elif is_required:
                unmapped_required.append(f"{label_text or field_id} ({reason})")
            else:
                skipped_optional.append(label_text or field_id)
        elif resolution["type"] == "decline_option":
            ok, reason = _select_decline_option(page, el)
            if ok:
                filled.append(label_text or field_id)
            elif is_required:
                unmapped_required.append(f"{label_text or field_id} ({reason})")
            else:
                skipped_optional.append(label_text or field_id)

    return {
        "filled": filled, "unmapped_required": unmapped_required, "skipped_optional": skipped_optional,
        "no_form_detected": False,
    }


def _select_by_label_group(page, group_selector: str, desired_bool: bool | None) -> tuple[bool, str]:
    """Plain HTML radio/checkbox groups (Ashby, Lever) — no dropdown to open,
    just pick the input whose own value or associated <label> text
    unambiguously matches yes/no. Same ambiguity-refusal principle as
    _select_react_option: only ever acts on a single, unambiguous match."""
    if desired_bool is None:
        return False, "no answer available for this field"

    inputs = page.locator(group_selector)
    count = inputs.count()
    if count == 0:
        return False, "no options found for this field"

    options = []
    for i in range(count):
        inp = inputs.nth(i)
        value_attr = (inp.get_attribute("value") or "").strip()
        input_id = inp.get_attribute("id")
        label_text = ""
        if input_id:
            label_el = page.locator(f'label[for="{input_id}"]')
            if label_el.count() > 0:
                label_text = label_el.inner_text().strip()
        text = value_attr or label_text
        options.append((text, inp))

    wanted_prefix = "yes" if desired_bool else "no"
    exact_matches = [(t, inp) for t, inp in options if t.lower() == wanted_prefix]
    prefix_matches = [(t, inp) for t, inp in options if t.lower().startswith(wanted_prefix)]

    target = None
    if exact_matches:
        target = exact_matches[0]
    elif len(prefix_matches) == 1:
        target = prefix_matches[0]

    if target is None:
        return False, f"ambiguous options for this field: {[t for t, _ in options]}"

    target[1].check()
    return True, ""


def _fill_ashby_application(page, resume: MasterResume, answers_bank: AnswersBank, docx_path) -> dict:
    """Ashby-hosted postings (jobs.ashbyhq.com) — a distinct platform from
    Greenhouse with its own stable system-field ids. The job page shows a
    description first; the actual form only appears after clicking "Apply
    for this Job", which this function does itself before looking for
    fields (never a submit click — that stays a separate, later action)."""
    if page.locator("#_systemfield_name").count() == 0:
        apply_link = page.get_by_role("link", name=re.compile("apply for this job", re.I))
        if apply_link.count() == 0:
            apply_link = page.get_by_role("button", name=re.compile("apply for this job", re.I))
        if apply_link.count() == 0:
            return {"filled": [], "unmapped_required": [], "skipped_optional": [], "no_form_detected": True}
        apply_link.first.click()
        page.wait_for_timeout(2000)

    if page.locator("#_systemfield_name").count() == 0:
        return {"filled": [], "unmapped_required": [], "skipped_optional": [], "no_form_detected": True}

    filled, unmapped_required, skipped_optional = [], [], []

    name_input = page.locator("#_systemfield_name")
    if resume.identity.name:
        name_input.fill(resume.identity.name)
        filled.append("name")

    email_input = page.locator("#_systemfield_email")
    if resume.identity.email:
        email_input.fill(resume.identity.email)
        filled.append("email")

    phone_input = page.locator('input[type="tel"]')
    phone_question_id = phone_input.first.get_attribute("id") if phone_input.count() > 0 else None
    if phone_input.count() > 0 and resume.identity.phone:
        phone_input.first.fill(resume.identity.phone)
        filled.append("phone")

    location_input = page.locator("#_systemfield_location")
    if location_input.count() > 0 and resume.identity.location:
        location_input.fill(resume.identity.location)
        filled.append("location")

    resume_input = page.locator("#_systemfield_resume")
    if resume_input.count() > 0:
        if docx_path is not None and Path(docx_path).exists():
            resume_input.set_input_files(str(docx_path))
            filled.append("resume")
        else:
            unmapped_required.append("resume (no resume file on file — upload one via the intake page)")

    # Custom questions: label.ashby-application-form-question-title[for=ID]
    # carries the question text; required-ness is marked by a CSS module
    # class containing "_required_" (a substring check, not an exact
    # compiled hash, since that hash can change between Ashby deployments).
    # A question's actual input(s) may share the label's `for` id directly
    # (text/textarea/select) or have ids merely CONTAINING it (radio/
    # checkbox groups, e.g. "{formId}_{questionId}-labeled-radio-0").
    labels = page.locator("label.ashby-application-form-question-title").all()
    seen_question_ids = {"_systemfield_name", "_systemfield_email", "_systemfield_resume", "_systemfield_location"}
    if phone_question_id:
        seen_question_ids.add(phone_question_id)
    for label_el in labels:
        question_id = label_el.get_attribute("for")
        if not question_id or question_id in seen_question_ids:
            continue
        seen_question_ids.add(question_id)
        label_text = label_el.inner_text().strip()
        css_class = label_el.get_attribute("class") or ""
        is_required = "_required_" in css_class

        # Attribute selector, not #id — Ashby's question ids are UUIDs that
        # can start with a digit, which is invalid CSS id-selector syntax.
        direct_input = page.locator(f'[id="{question_id}"]')
        group_inputs = page.locator(f'input[id*="{question_id}"]')

        resolution = _resolve_question(label_text, resume, answers_bank)
        if resolution is None:
            (unmapped_required if is_required else skipped_optional).append(label_text or question_id)
            continue

        if resolution["type"] == "text":
            if resolution["value"] and direct_input.count() > 0:
                direct_input.fill(resolution["value"])
                filled.append(label_text or question_id)
            elif is_required:
                unmapped_required.append(label_text or question_id)
            else:
                skipped_optional.append(label_text or question_id)
        elif resolution["type"] in ("boolean", "decline_option"):
            desired = resolution["value"] if resolution["type"] == "boolean" else None
            ok, reason = (
                _select_by_label_group(page, f'input[id*="{question_id}"]', desired)
                if resolution["type"] == "boolean"
                else (False, "decline-option questions aren't auto-answered on Ashby forms")
            )
            if ok:
                filled.append(label_text or question_id)
            elif is_required:
                unmapped_required.append(f"{label_text or question_id} ({reason})")
            else:
                skipped_optional.append(label_text or question_id)

    return {
        "filled": filled, "unmapped_required": unmapped_required, "skipped_optional": skipped_optional,
        "no_form_detected": False,
    }


def _fill_lever_application(page, resume: MasterResume, answers_bank: AnswersBank, docx_path) -> dict:
    """Lever-hosted postings (jobs.lever.co) — plain HTML form fields
    identified by `name` attribute, with a cookie-consent banner common
    enough to dismiss unconditionally before looking for anything else
    (dismissing a cookie banner is not "defeating bot detection" — it is
    not a bot-detection mechanism at all, just an EU/UK privacy-law
    disclosure). The apply form lives at the posting URL + "/apply", which
    this function navigates to itself if not already there."""
    deny_button = page.get_by_role("button", name=re.compile("^deny$", re.I))
    if deny_button.count() > 0:
        deny_button.first.click()
        page.wait_for_timeout(500)

    if page.locator('input[name="name"]').count() == 0:
        apply_link = page.get_by_role("link", name=re.compile("^apply$", re.I))
        if apply_link.count() > 0:
            href = apply_link.first.get_attribute("href")
            if href:
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                deny_button = page.get_by_role("button", name=re.compile("^deny$", re.I))
                if deny_button.count() > 0:
                    deny_button.first.click()
                    page.wait_for_timeout(500)

    if page.locator('input[name="name"]').count() == 0:
        return {"filled": [], "unmapped_required": [], "skipped_optional": [], "no_form_detected": True}

    filled, unmapped_required, skipped_optional = [], [], []

    _LEVER_STANDARD_FIELDS = {
        "name": lambda r, ab: r.identity.name,
        "email": lambda r, ab: r.identity.email,
        "phone": lambda r, ab: r.identity.phone,
        "location": lambda r, ab: r.identity.location,
        "urls[LinkedIn]": lambda r, ab: r.identity.linkedin or ab.linkedin_url,
        "urls[GitHub]": lambda r, ab: r.identity.github,
        "urls[Portfolio]": lambda r, ab: r.identity.portfolio,
    }
    for field_name, resolver in _LEVER_STANDARD_FIELDS.items():
        locator = page.locator(f'input[name="{field_name}"]')
        if locator.count() == 0:
            continue
        value = resolver(resume, answers_bank)
        if value:
            locator.first.fill(value)
            filled.append(field_name)

    resume_input = page.locator("#resume-upload-input, input[name='resume']")
    if resume_input.count() > 0:
        if docx_path is not None and Path(docx_path).exists():
            resume_input.first.set_input_files(str(docx_path))
            filled.append("resume")
        else:
            unmapped_required.append("resume (no resume file on file — upload one via the intake page)")

    # Custom questions: a wrapper <div> per question holds an
    # .application-label (question text, "✱"-marked when required) and a
    # sibling .application-field (the actual input/select/radio group) —
    # confirmed against a live posting, no per-input `for`/`id` linkage.
    label_divs = page.locator(".application-label").all()
    for label_div in label_divs:
        text_el = label_div.locator(".text")
        full_text = (text_el.inner_text() if text_el.count() > 0 else label_div.inner_text()).strip()
        is_required = "✱" in full_text
        label_text = full_text.replace("✱", "").strip()

        wrapper = label_div.locator("xpath=..")
        field = wrapper.locator(".application-field")
        if field.count() == 0:
            continue

        text_input = field.locator("input[type='text'], input:not([type]), textarea")
        select_input = field.locator("select")
        radio_or_checkbox = field.locator('input[type="radio"], input[type="checkbox"]')

        # Skip questions already handled as a standard field above (name,
        # email, phone, location, urls[...], resume) — they share the same
        # .application-label/.application-field structure as custom ones.
        # Checked against ALL inputs regardless of type (file/email/tel
        # included, not just the narrowly-typed text_input locator above —
        # missing this let the resume file input and email input slip
        # through as unrecognized "custom questions").
        already_handled_names = set(_LEVER_STANDARD_FIELDS.keys()) | {"resume"}
        all_field_inputs = field.locator("input, textarea, select")
        contained_names = {all_field_inputs.nth(i).get_attribute("name") for i in range(all_field_inputs.count())}
        if contained_names & already_handled_names:
            continue

        resolution = _resolve_question(label_text, resume, answers_bank)
        if resolution is None:
            (unmapped_required if is_required else skipped_optional).append(label_text)
            continue

        if resolution["type"] == "text":
            target = text_input if text_input.count() > 0 else select_input
            if resolution["value"] and target.count() > 0:
                if select_input.count() > 0 and target is select_input:
                    target.first.select_option(label=resolution["value"])
                else:
                    target.first.fill(resolution["value"])
                filled.append(label_text)
            elif is_required:
                unmapped_required.append(label_text)
            else:
                skipped_optional.append(label_text)
        elif resolution["type"] in ("boolean", "decline_option"):
            if resolution["type"] == "boolean" and radio_or_checkbox.count() > 0:
                ok, reason = _select_by_label_group(
                    page, f'input[name="{radio_or_checkbox.first.get_attribute("name")}"]', resolution["value"]
                )
            else:
                ok, reason = False, "decline-option questions aren't auto-answered on Lever forms"
            if ok:
                filled.append(label_text)
            elif is_required:
                unmapped_required.append(f"{label_text} ({reason})")
            else:
                skipped_optional.append(label_text)

    return {
        "filled": filled, "unmapped_required": unmapped_required, "skipped_optional": skipped_optional,
        "no_form_detected": False,
    }


def fill_application(page, resume: MasterResume, answers_bank: AnswersBank, docx_path) -> dict:
    """Fill every field we can confidently and truthfully answer. Returns a
    summary: {filled: [...], unmapped_required: [...], skipped_optional: [...],
    no_form_detected: bool}. Never touches a submit button. Dispatches to
    the right platform's field-mapping strategy by URL — Ashby and Lever
    each use a completely different DOM structure and field-naming
    convention than the classic Greenhouse embed."""
    url = page.url or ""
    if "ashbyhq.com" in url:
        return _fill_ashby_application(page, resume, answers_bank, docx_path)
    if "jobs.lever.co" in url:
        return _fill_lever_application(page, resume, answers_bank, docx_path)
    return _fill_greenhouse_application(page, resume, answers_bank, docx_path)
