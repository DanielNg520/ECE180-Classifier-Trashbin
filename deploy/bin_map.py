"""Collapse the 30 model classes into the 4 physical bins of the sorter.

The sorter has 4 bins in a round casing around a rotating pole. Each model
label maps to exactly one bin (0-3). Edit LABEL_TO_BIN to reclassify an item;
BIN_NAMES is only for logging/dashboard.

Bin layout (see memory: sorting-mechanism):
    0  Paper & Cardboard
    1  Containers  (metal cans, glass, plastic bottles/tubs)
    2  Organic / Compost
    3  Landfill / Trash  (film plastic, styrofoam, textiles, aerosols)
"""

BIN_NAMES = {
    0: "paper",
    1: "containers",
    2: "organic",
    3: "landfill",
}

LABEL_TO_BIN = {
    # 0 — Paper & Cardboard
    "cardboard_boxes": 0,
    "cardboard_packaging": 0,
    "magazines": 0,
    "newspaper": 0,
    "office_paper": 0,
    "paper_cups": 0,  # NB: plastic-lined cups are arguably landfill — move if desired
    # 1 — Containers (metal / glass / rigid recyclable plastic)
    "aluminum_food_cans": 1,
    "aluminum_soda_cans": 1,
    "steel_food_cans": 1,
    "glass_beverage_bottles": 1,
    "glass_cosmetic_containers": 1,
    "glass_food_jars": 1,
    "plastic_detergent_bottles": 1,
    "plastic_food_containers": 1,
    "plastic_soda_bottles": 1,
    "plastic_water_bottles": 1,
    # 2 — Organic / Compost
    "coffee_grounds": 2,
    "eggshells": 2,
    "food_waste": 2,
    "tea_bags": 2,
    # 3 — Landfill / Trash
    "aerosol_cans": 3,  # pressurized — safest in landfill for a student rig
    "clothing": 3,
    "disposable_plastic_cutlery": 3,
    "plastic_cup_lids": 3,
    "plastic_shopping_bags": 3,
    "plastic_straws": 3,
    "plastic_trash_bags": 3,
    "shoes": 3,
    "styrofoam_cups": 3,
    "styrofoam_food_containers": 3,
}

# Where unmapped/unknown labels go (should never happen if labels.txt matches).
DEFAULT_BIN = 3


def label_to_bin(label):
    """Return the physical bin index (0-3) for a model label."""
    return LABEL_TO_BIN.get(label, DEFAULT_BIN)


if __name__ == "__main__":
    # Sanity check against export/labels.txt when run directly.
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "export", "labels.txt")) as f:
        labels = [ln.strip() for ln in f if ln.strip()]
    missing = [l for l in labels if l not in LABEL_TO_BIN]
    print(f"{len(labels)} labels, {len(missing)} unmapped: {missing or 'none'}")
    for b in range(4):
        n = sum(1 for v in LABEL_TO_BIN.values() if v == b)
        print(f"  bin {b} {BIN_NAMES[b]:12s} {n} labels")
