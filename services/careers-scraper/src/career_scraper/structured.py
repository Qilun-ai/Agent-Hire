"""Read JSON data embedded in public pages without evaluating JavaScript."""
import json
import re

from bs4 import BeautifulSoup


def embedded_page_object(html, marker):
    """Decode a literal JSON object after an inspected script marker; never eval JS."""
    start = html.find(marker)
    if start < 0:
        raise ValueError(f'Native page object missing: {marker}')
    try:
        result, _ = json.JSONDecoder().raw_decode(html[start+len(marker):].lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError('Native page data is not the inspected JSON schema') from exc
    if not isinstance(result, dict):
        raise ValueError('Native page data is not an object')
    return result


def next_flight_values(html):
    chunks = []
    decoder = json.JSONDecoder()
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.get_text()
        for match in re.finditer(r"self\.__next_f\.push\(", text):
            try:
                args, _ = decoder.raw_decode(text[match.end():])
            except ValueError:
                continue
            if isinstance(args, list) and len(args) == 2 and args[0] == 1 and isinstance(args[1], str):
                chunks.append(args[1])
    stream = "".join(chunks)
    values = []
    for match in re.finditer(r"(?:^|\n)[0-9a-f]+:", stream):
        try:
            value, _ = decoder.raw_decode(stream[match.end():])
            values.append(value)
        except ValueError:
            # Module references, text frames and debug records are not JSON values.
            continue
    return values


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)
