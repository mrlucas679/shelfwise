from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Subcat:
    name: str
    physics: str
    pack: str
    base_c: int
    brand_pool: str = "generic"
    vat: str = "standard"
    vegan: bool = False
    gluten_free: bool = False
    allergens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    subs: tuple[Subcat, ...]


@dataclass(frozen=True, slots=True)
class Department:
    name: str
    aisle: str
    cats: tuple[Category, ...]


def iter_subcats():
    """Yield every department, category, and subcategory row."""
    for department in TAXONOMY:
        for category in department.cats:
            for subcat in category.subs:
                yield department, category, subcat


def _s(
    name: str,
    physics: str,
    pack: str,
    base_c: int,
    pool: str = "generic",
    vat: str = "standard",
    vegan: bool = False,
    gf: bool = False,
    allg: tuple[str, ...] = (),
) -> Subcat:
    """Create a compact subcategory definition."""
    return Subcat(name, physics, pack, base_c, pool, vat, vegan, gf, tuple(allg))


def _c(name: str, *subs: Subcat) -> Category:
    """Create a compact category definition."""
    return Category(name, tuple(subs))


CORE_TAXONOMY: tuple[Department, ...] = (
    Department(
        "Fresh Produce",
        "A1",
        (
            _c(
                "Fruit",
                _s("Apples", "produce", "produce_wt", 2500, vat="zero", vegan=True, gf=True),
                _s("Bananas", "produce", "produce_wt", 1800, vat="zero", vegan=True, gf=True),
                _s("Citrus", "produce", "produce_wt", 2200, vat="zero", vegan=True, gf=True),
            ),
            _c(
                "Vegetables",
                _s("Root Vegetables", "produce", "produce_wt", 2000, vat="zero", vegan=True),
                _s("Tomatoes", "produce", "produce_wt", 2400, vat="zero", vegan=True),
                _s("Leafy Vegetables", "produce", "produce_wt", 1500, vat="zero", vegan=True),
            ),
        ),
    ),
    Department(
        "Bakery",
        "A2",
        (
            _c(
                "Bread",
                _s("Brown Bread", "bakery", "unit", 1700, "bakery", vat="zero", allg=("gluten",)),
                _s("White Bread", "bakery", "unit", 1900, "bakery", allg=("gluten",)),
                _s("Rolls & Buns", "bakery", "unit", 2200, "bakery", allg=("gluten",)),
            ),
        ),
    ),
    Department(
        "Butchery",
        "A3",
        (
            _c(
                "Meat",
                _s("Beef", "meat", "kg_meat", 9900, vat="zero"),
                _s("Mince", "meat", "kg_meat", 7900, vat="zero"),
                _s("Lamb", "meat", "kg_meat", 14900, vat="zero"),
            ),
            _c(
                "Poultry",
                _s("Fresh Chicken", "poultry", "kg_meat", 6500, vat="zero"),
                _s("Chicken Portions", "poultry", "kg_meat", 5900, vat="zero"),
            ),
            _c("Seafood", _s("Fresh Fish", "seafood", "kg_meat", 11900)),
        ),
    ),
    Department(
        "Dairy & Eggs",
        "B1",
        (
            _c(
                "Milk",
                _s("Fresh Milk", "dairy", "milk", 2200, "dairy", vat="zero", allg=("milk",)),
                _s("UHT Milk", "dairy", "milk", 2000, "dairy", vat="zero", allg=("milk",)),
                _s("Amasi", "dairy", "milk", 2400, "dairy", vat="zero", allg=("milk",)),
            ),
            _c(
                "Dairy",
                _s("Cheese", "dairy", "g_small", 5500, "dairy", allg=("milk",)),
                _s("Yoghurt", "dairy", "g_small", 1900, "dairy", allg=("milk",)),
                _s("Butter", "dairy", "g_small", 6500, "dairy", allg=("milk",)),
            ),
            _c("Eggs", _s("Eggs", "eggs", "unit", 5500, vat="zero", allg=("egg",))),
        ),
    ),
    Department(
        "Frozen Foods",
        "B2",
        (
            _c(
                "Frozen",
                _s("Frozen Chicken", "frozen", "kg_meat", 6500, vat="zero"),
                _s("Frozen Vegetables", "frozen", "kg_staple", 3500, vat="zero", vegan=True),
                _s("Ice Cream", "frozen", "ml_liquid", 4900, allg=("milk",)),
            ),
        ),
    ),
    Department(
        "Pantry",
        "C1",
        (
            _c(
                "Staples",
                _s("Maize Meal", "ambient_long", "kg_staple", 8500, "staple", vat="zero"),
                _s("Rice", "ambient_long", "kg_staple", 5500, "staple", vat="zero"),
                _s("Samp", "ambient_long", "kg_staple", 4500, "staple", vat="zero"),
                _s("Flour", "ambient_long", "kg_staple", 4900, "staple", vat="zero"),
                _s("Sugar", "ambient_long", "kg_staple", 5500, "staple"),
            ),
            _c(
                "Tinned",
                _s("Tinned Pilchards", "ambient_long", "g_small", 2200, "canned", vat="zero"),
                _s("Tinned Beans", "ambient_long", "g_small", 1700, "canned", vegan=True),
                _s("Tinned Tomatoes", "ambient_long", "g_small", 1500, "canned", vegan=True),
            ),
        ),
    ),
    Department(
        "Soft Drinks",
        "D1",
        (
            _c(
                "Carbonates",
                _s("Cola", "beverage", "can_bottle", 2200, "soft_drink", vegan=True),
                _s("Lemonade", "beverage", "can_bottle", 2000, "soft_drink", vegan=True),
                _s("Ginger Beer", "beverage", "can_bottle", 2100, "soft_drink", vegan=True),
            ),
        ),
    ),
    Department(
        "Household Cleaning",
        "F1",
        (
            _c(
                "Cleaning",
                _s("Dishwashing Liquid", "nonfood", "ml_liquid", 3200, "cleaning"),
                _s("Bleach", "nonfood", "ml_liquid", 2500, "cleaning"),
                _s("Washing Powder", "nonfood", "kg_staple", 8900, "cleaning"),
            ),
        ),
    ),
)

_EXTRA_DEPARTMENT_NAMES = (
    "Juices",
    "Water",
    "Tea",
    "Coffee",
    "Snacks",
    "Confectionery",
    "Biscuits",
    "Breakfast Foods",
    "Baby Products",
    "Health Care",
    "Beauty",
    "Personal Care",
    "Laundry",
    "Paper Products",
    "Pet Care",
    "Deli",
    "Ready Meals",
    "Stationery",
    "Kitchenware",
    "Homeware",
    "Seasonal Products",
    "International Foods",
    "Health Foods",
    "Bulk Foods",
    "Clothing",
    "Footwear",
    "Electronics",
    "Hardware",
    "Garden",
    "Automotive",
    "Toys",
    "Books & Magazines",
    "Pharmacy",
    "Floral",
    "Gift Cards",
    "Tobacco",
    "Alcohol",
    "Outdoor",
)


def _extra_departments() -> tuple[Department, ...]:
    """Generate broad non-core departments without hand-maintaining repetitive rows."""
    departments: list[Department] = []
    for index, name in enumerate(_EXTRA_DEPARTMENT_NAMES, start=1):
        physics = "health" if name in {"Health Care", "Pharmacy", "Beauty"} else "nonfood"
        pack = "ml_liquid" if name in {"Juices", "Water", "Tea", "Coffee", "Alcohol"} else "unit"
        base = 2500 + index * 300
        departments.append(
            Department(
                name,
                f"X{index}",
                (
                    _c(
                        name,
                        _s(f"{name} Standard", physics, pack, base),
                        _s(f"{name} Premium", physics, pack, int(base * 1.6)),
                    ),
                ),
            )
        )
    return tuple(departments)


TAXONOMY = CORE_TAXONOMY + _extra_departments()
