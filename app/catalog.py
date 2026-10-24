"""Справочник видов для формы «Новое растение».

Условия ухода берутся из начального списка (seed.py), тексты — из facts.py, diseases.py
и repotting.py. Ключ — начало латинского названия, как в этих справочниках.
"""
import re

from app.seed import PLANTS, REFERENCE, reference_text

# что подставляется из начального списка; название, место и личные заметки у каждого растения свои
CARE_FIELDS = ("water_summer_days", "water_winter_days", "water_ml", "water_how", "soil",
               "temperature", "light", "air_humidity", "feed_days", "repot_years", "spray_days",
               "wipe_days", "toxic")


def _build() -> dict[str, dict]:
    catalog = {}
    for key in dict.fromkeys(k for texts in REFERENCE.values() for k in texts):
        seed = next((p for p in PLANTS if p.get("species", "").startswith(key)), {})
        # «Фикус Бенджамина «Эстер»» -> «Фикус Бенджамина»: сорт и пометки в скобках — не про вид
        name = re.sub(r"\s*[(«].*$", "", seed.get("name", "")) or key
        values = {"name": name, "species": key, **{f: seed.get(f) for f in CARE_FIELDS if seed.get(f) is not None}}
        values.update({f: reference_text(f, key) for f in REFERENCE})
        catalog[key] = {"label": f"{name} · {key}", "values": values}
    return dict(sorted(catalog.items(), key=lambda item: item[1]["label"]))


CATALOG = _build()
