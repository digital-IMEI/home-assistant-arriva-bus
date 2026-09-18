"""Frame original screenshots in SVG without redrawing the captured interface."""

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def card(filename, y, height, x=60, top=0, width=1080):
    data = base64.b64encode((ROOT / 'docs/images' / filename).read_bytes()).decode()
    return (
        f'<svg x="{x}" y="{top}" width="{width}" height="{width * height / 706}" '
        f'viewBox="0 {y} 706 {height}">'
        f'<image width="706" height="1536" href="data:image/jpeg;base64,{data}"/>'
        '</svg>'
    )


def document(height, content):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" '
        f'viewBox="0 0 1200 {height}">'
        '<rect width="1200" height="100%" rx="28" fill="#101825"/>'
        '<g font-family="Arial, sans-serif">' + content + '</g></svg>'
    )


hero = document(605,
    '<text x="60" y="64" fill="#48c9d4" font-size="20" letter-spacing="3">ARRIVA BUS · HOME ASSISTANT</text>'
    '<text x="60" y="127" fill="#ffffff" font-size="44" font-weight="700">Your next bus. A glance away.</text>'
    '<text x="60" y="174" fill="#b4c1d1" font-size="24">Live delay, last passed stop and route progress on your iPhone.</text>'
    + card('live-activity-delayed.jpeg', 1090, 222, top=207)
)
gallery = document(1175,
    '<text x="60" y="65" fill="#ffffff" font-size="34" font-weight="700">From waiting to underway</text>'
    '<text x="60" y="107" fill="#b4c1d1" font-size="22">One Live Activity that updates as your bus moves.</text>'
    '<text x="60" y="169" fill="#b4c1d1" font-size="21">01 / WAITING TO DEPART</text>'
    + card('live-activity-waiting.jpeg', 1168, 147, top=193)
    + '<text x="60" y="477" fill="#ffffff" font-size="21">02 / ON TIME</text>'
    + card('live-activity-on-time.jpeg', 1074, 225, top=501)
    + '<text x="60" y="907" fill="#ff746a" font-size="21">03 / DYNAMIC ISLAND</text>'
)
# Keep the compact Dynamic Island capture at its original aspect ratio.
island = base64.b64encode((ROOT / 'docs/images/dynamic-island-delayed.jpeg').read_bytes()).decode()
gallery = gallery.replace('</g></svg>',
    f'<image x="60" y="935" width="1080" height="183" href="data:image/jpeg;base64,{island}"/></g></svg>')
(ROOT / 'docs/images/live-activity-hero.svg').write_text(hero)
(ROOT / 'docs/images/live-activity-gallery.svg').write_text(gallery)
