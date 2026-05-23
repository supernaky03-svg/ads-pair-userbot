from __future__ import annotations


def build_report_message(*, day_number: int, channel_link: str, post_links: list[str]) -> str:
    if len(post_links) == 1:
        return f"Day{day_number}\n{channel_link}\n{post_links[0]}"
    lines = [f"Day{day_number}", channel_link, ""]
    lines.extend(f"{idx}. {link}" for idx, link in enumerate(post_links, start=1))
    return "\n".join(lines)
