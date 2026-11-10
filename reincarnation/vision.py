from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

# Results and bus prompts take priority over HUD visible behind overlays.
PRIORITY = ("searching", "loading", "results", "xp", "continue", "spectating",
            "eliminated", "downed", "lobby", "bus", "bus_wait", "waiting", "deploying", "match")


@dataclass(frozen=True)
class Observation:
    state: str = "unknown"
    score: float = 0.0
    detail: str = "no matching template"
    template: str | None = None
    action: dict | None = None


def text_mask(image: Image.Image, metric: str) -> Image.Image:
    channels = image.convert("RGB").split()
    lo = ImageChops.darker(ImageChops.darker(channels[0], channels[1]), channels[2])
    hi = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    if metric == "dark_text":
        return hi.point(lambda n: 255 if n < 75 else 0)
    bright = lo.point(lambda n: 255 if n >= 190 else 0)
    neutral = ImageChops.subtract(hi, lo).point(lambda n: 255 if n < 65 else 0)
    return ImageChops.multiply(bright, neutral)


class Region:
    def __init__(self, spec: dict, root: Path):
        self.box = tuple(spec["box"])
        self.threshold = spec["threshold"]
        self.metric = spec.get("metric", "rgb")
        with Image.open(root / spec["file"]) as im:
            self.reference = im.convert("RGB")
        if self.reference.size != (self.box[2] - self.box[0], self.box[3] - self.box[1]):
            raise ValueError(f"Template size mismatch: {spec['file']}")
        self.mask = None
        if self.metric != "rgb":
            self.mask = text_mask(self.reference, self.metric)
            self.area = sum(self.mask.histogram()[1:])
            if self.area < 10:
                raise ValueError(f"Insufficient text evidence: {spec['file']}")
            self.dilated = self.mask.filter(ImageFilter.MaxFilter(3))

    def score(self, frame: Image.Image) -> float:
        patch = frame.crop(self.box).convert("RGB")
        if self.mask is None:
            rms = ImageStat.Stat(ImageChops.difference(patch, self.reference)).rms
            return 1.0 - sum(rms) / (3 * 255)
        mask = text_mask(patch, self.metric)
        area = sum(mask.histogram()[1:])
        if area < 10:
            return 0.0
        # Symmetric coverage tolerates one pixel of antialiasing/animation and
        # penalizes extra scene details as well as missing letters.
        precision = sum(ImageChops.multiply(mask, self.dilated).histogram()[1:]) / area
        recall = sum(ImageChops.multiply(self.mask, mask.filter(ImageFilter.MaxFilter(3))).histogram()[1:]) / self.area
        return (precision * recall) ** .5


class Recognizer:
    def __init__(self, profile: dict, root: Path):
        self.size = tuple(profile["size"]) if profile["size"] else None
        self.templates = []
        for spec in profile["templates"]:
            regions = [Region(s, root) for s in [spec, *spec.get("checks", [])]]
            self.templates.append((spec, regions))

    def classify(self, frame: Image.Image) -> Observation:
        if frame.size != self.size:
            return Observation(detail=f"size mismatch: {frame.size}, expected {self.size}")
        matches = {}
        best = Observation()
        for spec, regions in self.templates:
            scores = [region.score(frame) for region in regions]
            score = min(scores)
            if score > best.score:
                best = Observation("unknown", score, f"nearest: {spec['state']}")
            if all(s >= region.threshold for s, region in zip(scores, regions)):
                old = matches.get(spec["state"])
                if old is None or score > old[0]:
                    matches[spec["state"]] = (score, spec)
        for state in PRIORITY:
            if state in matches:
                score, spec = matches[state]
                return Observation(state, score, spec["file"], spec["file"], spec.get("action"))
        return best
