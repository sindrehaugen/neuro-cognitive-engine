"""J.9 Outlook .msg and RFC 822 .eml with recursive attachments."""

from __future__ import annotations

import asyncio
import io
import logging
from email import policy
from email.parser import BytesParser
from typing import Any

from nce.extractors.common import html_to_text
from nce.extractors.core import ExtractionResult, Section, empty_skipped
from nce.extractors.dispatch import _MAX_ATTACHMENTS_PER_MESSAGE

log = logging.getLogger(__name__)


def _as_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)


def _msg_metadata(msg: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for attr, key in (
        ("subject", "subject"),
        ("date", "date"),
        ("sender", "sender"),
    ):
        try:
            v = getattr(msg, attr, None)
            if v is not None:
                meta[key] = str(v)
        except Exception:
            pass
    try:
        mid = getattr(msg, "messageId", None) or getattr(msg, "message_id", None)
        if mid:
            meta["messageId"] = str(mid)
    except Exception:
        pass
    return meta


async def extract_msg(blob: bytes) -> ExtractionResult:
    import os
    import tempfile

    from extract_msg import Message

    warnings: list[str] = []
    path: str | None = None
    msg = None
    try:
        fd, path = tempfile.mkstemp(suffix=".msg")
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(blob)
        msg = await asyncio.to_thread(Message, path)
    except Exception as e:
        log.warning("extract_msg open failed: %s", e)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        return empty_skipped("extract-msg", "corrupt", warnings=[str(e)])

    sections: list[Section] = []
    order = 0
    try:
        headers = (
            f"From: {getattr(msg, 'sender', '') or ''}\n"
            f"To: {getattr(msg, 'to', '') or ''}\n"
            f"Cc: {getattr(msg, 'cc', '') or ''}\n"
            f"Subject: {getattr(msg, 'subject', '') or ''}\n"
            f"Date: {getattr(msg, 'date', '') or ''}\n"
        )
        sections.append(
            Section(
                text=headers.strip(),
                structure_path="Headers",
                section_type="metadata",
                order=order,
            )
        )
        order += 1
    except Exception as e:
        warnings.append(f"msg_headers_partial:{e}")

    body_text = ""
    try:
        body_text = (getattr(msg, "body", None) or "").strip()
        if not body_text:
            html_b = getattr(msg, "htmlBody", None) or getattr(msg, "html_body", None)
            if html_b:
                if isinstance(html_b, bytes):
                    html_b = html_b.decode("utf-8", errors="replace")
                body_text = html_to_text(html_b)
    except Exception as e:
        warnings.append(f"msg_body_partial:{e}")

    sections.append(
        Section(
            text=body_text or "(no body)",
            structure_path="Body",
            section_type="body",
            order=order,
        )
    )
    order += 1

    attachments = []
    try:
        attachments = list(getattr(msg, "attachments", []) or [])
    except Exception as e:
        warnings.append(f"msg_attachments_list_failed:{e}")

    budget: list[int] = [0]
    for att in attachments:
        try:
            if budget[0] >= _MAX_ATTACHMENTS_PER_MESSAGE:
                warnings.append("attachment_limit")
                break
            data = getattr(att, "data", None)
            if data is None:
                data = att.get_bytes() if hasattr(att, "get_bytes") else None
            fname = (
                getattr(att, "longFilename", None)
                or getattr(att, "shortFilename", None)
                or getattr(att, "name", None)
                or "attachment"
            )
            if not data:
                warnings.append(f"attachment_empty:{fname}")
                continue
            from nce.extractors.dispatch import extract_with_fallback

            budget[0] += 1
            att_result = await extract_with_fallback(
                blob=data,
                filename=str(fname),
                mime_type=None,
                attachment_depth=1,
                attachment_budget=budget,
            )
            block = f"[Attachment: {fname}]\n\n{att_result.text}"
            if att_result.warnings:
                warnings.extend([f"att:{fname}:{w}" for w in att_result.warnings])
            sections.append(
                Section(
                    text=block,
                    structure_path=f"Attachment: {fname}",
                    section_type="attachment",
                    order=order,
                )
            )
            order += 1
        except Exception as e:
            warnings.append(f"attachment_extract_failed:{e}")
            log.warning("msg attachment failed: %s", e, exc_info=True)

    try:
        if msg is not None:
            msg.close()
    except Exception:
        pass
    if path:
        try:
            os.unlink(path)
        except OSError:
            warnings.append(f"temp_msg_unlink_failed:{path}")

    full = "\n\n".join(s.text for s in sections)
    return ExtractionResult(
        method="extract-msg",
        text=full,
        sections=sections,
        metadata=_msg_metadata(msg),
        warnings=warnings,
    )


def _eml_metadata(em: Any) -> dict[str, Any]:
    return {
        "subject": em.get("Subject"),
        "from": em.get("From"),
        "to": em.get("To"),
        "date": em.get("Date"),
    }


async def extract_eml(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        em = BytesParser(policy=policy.default).parse(io.BytesIO(blob))
    except Exception as e:
        log.warning("eml parse failed: %s", e)
        return empty_skipped("email", "corrupt", warnings=[str(e)])

    sections: list[Section] = []
    order = 0
    hdr_lines = []
    for k, v in em.items():
        try:
            hdr_lines.append(f"{k}: {v}")
        except Exception:
            hdr_lines.append(f"{k}: <unprintable>")
    if hdr_lines:
        sections.append(
            Section(
                text="\n".join(hdr_lines),
                structure_path="Headers",
                section_type="metadata",
                order=order,
            )
        )
        order += 1

    body_plain = ""
    body_html = ""
    try:
        if em.is_multipart():
            for part in em.walk():
                ctype = part.get_content_type()
                if part.get_filename():
                    continue
                if ctype == "text/plain":
                    try:
                        body_plain = _as_str(part.get_content()).strip()
                    except Exception as e:
                        warnings.append(f"eml_plain_part:{e}")
                elif ctype == "text/html" and not body_plain:
                    try:
                        raw = part.get_content()
                        body_html = html_to_text(_as_str(raw)) if raw else ""
                    except Exception as e:
                        warnings.append(f"eml_html_part:{e}")
        else:
            ctype = em.get_content_type()
            if ctype == "text/html":
                try:
                    body_html = html_to_text(_as_str(em.get_content()))
                except Exception as e:
                    warnings.append(f"eml_body_html:{e}")
            else:
                try:
                    body_plain = _as_str(em.get_content()).strip()
                except Exception as e:
                    warnings.append(f"eml_body_plain:{e}")
    except Exception as e:
        warnings.append(f"eml_body_walk:{e}")

    body = body_plain or body_html or "(no body)"
    sections.append(Section(text=body, structure_path="Body", section_type="body", order=order))
    order += 1

    budget: list[int] = [0]
    try:
        if em.is_multipart():
            for part in em.walk():
                fname = part.get_filename()
                if not fname:
                    continue
                if budget[0] >= _MAX_ATTACHMENTS_PER_MESSAGE:
                    warnings.append("attachment_limit")
                    break
                try:
                    ctype = part.get_content_type()
                    payload = part.get_payload(decode=True)
                    if not payload:
                        warnings.append(f"eml_att_empty:{fname}")
                        continue
                    from nce.extractors.dispatch import extract_with_fallback

                    budget[0] += 1
                    att_res = await extract_with_fallback(
                        blob=payload if isinstance(payload, bytes) else bytes(payload),
                        filename=fname,
                        mime_type=ctype,
                        attachment_depth=1,
                        attachment_budget=budget,
                    )
                    block = f"[Attachment: {fname}]\n\n{att_res.text}"
                    warnings.extend([f"att:{fname}:{w}" for w in att_res.warnings])
                    sections.append(
                        Section(
                            text=block,
                            structure_path=f"Attachment: {fname}",
                            section_type="attachment",
                            order=order,
                        )
                    )
                    order += 1
                except Exception as e:
                    warnings.append(f"eml_attachment_failed:{fname}:{e}")
                    log.warning("eml attachment %s failed: %s", fname, e, exc_info=True)
    except Exception as e:
        warnings.append(f"eml_attachments_walk:{e}")

    full = "\n\n".join(s.text for s in sections)
    return ExtractionResult(
        method="email",
        text=full,
        sections=sections,
        metadata=_eml_metadata(em),
        warnings=warnings,
    )
