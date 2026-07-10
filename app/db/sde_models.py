from sqlalchemy import Column, Integer, String, Float, Boolean, Text
from app.db.models import Base


class SDEType(Base):
    """invTypes — item type_id <-> name mapping."""
    __tablename__ = "sde_types"

    type_id = Column(Integer, primary_key=True)
    type_name = Column(String, nullable=False, index=True)
    group_id = Column(Integer, nullable=True)
    category_id = Column(Integer, nullable=True)
    market_group_id = Column(Integer, nullable=True, index=True)
    published = Column(Boolean, default=True)
    mass = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    capacity = Column(Float, nullable=True)
    portion_size = Column(Integer, nullable=True)


class SDESystem(Base):
    """mapSolarSystems — system info."""
    __tablename__ = "sde_systems"

    system_id = Column(Integer, primary_key=True)
    system_name = Column(String, nullable=False, index=True)
    security = Column(Float, nullable=True)
    constellation_id = Column(Integer, nullable=True)
    region_id = Column(Integer, nullable=True)


class SDEJump(Base):
    """mapSolarSystemJumps — jump graph edges for pathfinding."""
    __tablename__ = "sde_jumps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_system_id = Column(Integer, nullable=False, index=True)
    to_system_id = Column(Integer, nullable=False)


class SDEStation(Base):
    """staStations — NPC stations with cloning flag."""
    __tablename__ = "sde_stations"

    station_id = Column(Integer, primary_key=True)
    station_name = Column(String, nullable=False)
    system_id = Column(Integer, nullable=False, index=True)
    has_cloning = Column(Boolean, default=False, index=True)


class SDERegion(Base):
    """mapRegions — region id/name lookup."""
    __tablename__ = "sde_regions"

    region_id = Column(Integer, primary_key=True)
    region_name = Column(String, nullable=False, index=True)


class SDEConstellation(Base):
    """mapConstellations — constellation id/name lookup."""
    __tablename__ = "sde_constellations"

    constellation_id = Column(Integer, primary_key=True)
    constellation_name = Column(String, nullable=False)
    region_id = Column(Integer, nullable=True)


class SDEBlueprintMaterial(Base):
    """industryActivityMaterials — materials required to manufacture an item."""
    __tablename__ = "sde_blueprint_materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    blueprint_type_id = Column(Integer, nullable=False, index=True)
    activity_id = Column(Integer, nullable=False)   # 1 = manufacturing
    material_type_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)


class SDEMeta(Base):
    """Tracks SDE version and last update time."""
    __tablename__ = "sde_meta"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


class SDETypeMaterial(Base):
    """invTypeMaterials — reprocessing outputs for ore/item types."""
    __tablename__ = "sde_type_materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_id = Column(Integer, nullable=False, index=True)
    material_type_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)


class SDECompressible(Base):
    """compressibleTypes — maps raw ore type_id to compressed type_id."""
    __tablename__ = "sde_compressible"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_id = Column(Integer, nullable=False, index=True)
    compressed_type_id = Column(Integer, nullable=False, index=True)


class SDEBlueprintInfo(Base):
    """Blueprint manufacturing time and product mapping."""
    __tablename__ = "sde_blueprint_info"

    blueprint_type_id = Column(Integer, primary_key=True)
    product_type_id = Column(Integer, nullable=True, index=True)
    manufacturing_time = Column(Integer, nullable=True)  # seconds
    product_quantity = Column(Integer, nullable=True, default=1)


class SDEBlueprintInvention(Base):
    """Invention activity info — T1 blueprint -> invented T2 blueprint."""
    __tablename__ = "sde_blueprint_invention"

    blueprint_type_id = Column(Integer, primary_key=True)  # T1 blueprint being invented FROM
    product_blueprint_type_id = Column(Integer, nullable=True, index=True)  # T2 blueprint produced
    probability = Column(Float, nullable=True)
    base_runs = Column(Integer, nullable=True)
    time = Column(Integer, nullable=True)  # seconds


class SDEBlueprintInventionMaterial(Base):
    """Datacores (and other invention materials) consumed per invention attempt."""
    __tablename__ = "sde_blueprint_invention_materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    blueprint_type_id = Column(Integer, nullable=False, index=True)
    material_type_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)


class SDEBlueprintInventionSkill(Base):
    """Skills (encryption + science) required for an invention attempt."""
    __tablename__ = "sde_blueprint_invention_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    blueprint_type_id = Column(Integer, nullable=False, index=True)
    skill_type_id = Column(Integer, nullable=False)


# ── Skill planning SDE tables ───────────────────────────────────────────────

class SDEGroup(Base):
    """invGroups — item group id/name/category lookup."""
    __tablename__ = "sde_groups"

    group_id = Column(Integer, primary_key=True)
    category_id = Column(Integer, nullable=True, index=True)
    group_name = Column(String, nullable=False)


class SDETypeSkillReq(Base):
    """Skill requirements extracted from typeDogma — what skills an item/ship needs."""
    __tablename__ = "sde_type_skill_reqs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_id = Column(Integer, nullable=False, index=True)
    skill_type_id = Column(Integer, nullable=False)
    required_level = Column(Integer, nullable=False)


class SDESkillInfo(Base):
    """Skill metadata extracted from typeDogma — primary/secondary attrs + rank."""
    __tablename__ = "sde_skill_info"

    type_id = Column(Integer, primary_key=True)
    primary_attr = Column(Integer, nullable=False)    # 164=cha,165=int,166=mem,167=per,168=wil
    secondary_attr = Column(Integer, nullable=False)
    rank = Column(Float, nullable=False, default=1.0)


class SDECertificate(Base):
    """EVE certificates (mastery building blocks)."""
    __tablename__ = "sde_certificates"

    certificate_id = Column(Integer, primary_key=True)
    group_id = Column(Integer, nullable=True, index=True)
    name = Column(String, nullable=False)


class SDECertificateSkill(Base):
    """Skills required per certificate at each mastery tier."""
    __tablename__ = "sde_certificate_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    certificate_id = Column(Integer, nullable=False, index=True)
    skill_type_id = Column(Integer, nullable=False)
    basic = Column(Integer, default=0)       # Mastery I
    standard = Column(Integer, default=0)    # Mastery II
    improved = Column(Integer, default=0)    # Mastery III
    advanced = Column(Integer, default=0)    # Mastery IV
    elite = Column(Integer, default=0)       # Mastery V


class SDEShipMastery(Base):
    """Maps ships to certificate IDs per mastery level."""
    __tablename__ = "sde_ship_masteries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ship_type_id = Column(Integer, nullable=False, index=True)
    mastery_level = Column(Integer, nullable=False)  # 0-4 (I-V)
    certificate_id = Column(Integer, nullable=False)


# ── Planetary Industry SDE tables ───────────────────────────────────────────

class SDEPlanet(Base):
    """mapDenormalize (planets only) — which planets exist in which systems.

    `planet_type_id` is the SDE invType id for the planet type (e.g. 2016 Barren).
    `planet_index` is the Roman-numeral ordinal within the system (I, II, III…).
    """
    __tablename__ = "sde_planets"

    planet_id = Column(Integer, primary_key=True)
    system_id = Column(Integer, nullable=False, index=True)
    planet_type_id = Column(Integer, nullable=False, index=True)
    planet_name = Column(String, nullable=False)
    planet_index = Column(Integer, nullable=True)
    radius = Column(Float, nullable=True)
    distance_au = Column(Float, nullable=True)  # orbital distance from star in AU


class SDEPlanetSchematic(Base):
    """planetSchematics — PI recipes (name + cycle time)."""
    __tablename__ = "sde_planet_schematics"

    schematic_id = Column(Integer, primary_key=True)
    schematic_name = Column(String, nullable=False)
    cycle_time = Column(Integer, nullable=True)   # seconds


class SDEPlanetSchematicMaterial(Base):
    """planetSchematicsTypeMap — inputs/outputs for each PI schematic."""
    __tablename__ = "sde_planet_schematic_materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schematic_id = Column(Integer, nullable=False, index=True)
    type_id = Column(Integer, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    is_input = Column(Boolean, nullable=False, index=True)  # True = material, False = product


# ── Wormhole reference SDE tables ──────────────────────────────────────────

class SDEWormholeClass(Base):
    """mapLocationWormholeClasses — maps location IDs to wormhole class.

    location_id can be a system_id, constellation_id, or region_id.
    Class mapping: 1-6=C1-C6, 7=HS, 8=LS, 9=NS, 12=Thera, 13=C13/shattered,
    14-18=drifter, 25=Pochven.
    """
    __tablename__ = "sde_wormhole_classes"

    location_id = Column(Integer, primary_key=True)
    wormhole_class_id = Column(Integer, nullable=False, index=True)


class SDEWormholeType(Base):
    """Wormhole connection types with dogma attributes (group 988).

    Stores mass, lifetime, destination, and other properties extracted from
    typeDogma for each wormhole type item.
    """
    __tablename__ = "sde_wormhole_types"

    type_id = Column(Integer, primary_key=True)
    type_name = Column(String, nullable=False, index=True)
    target_class = Column(Integer, nullable=True)         # destination WH class (dogma 1381)
    max_stable_mass = Column(Float, nullable=True)        # total mass in kg (dogma 1382)
    max_stable_time = Column(Float, nullable=True)        # lifetime in minutes (dogma 1383)
    mass_regen = Column(Float, nullable=True)             # mass regen in kg (dogma 1384)
    max_jump_mass = Column(Float, nullable=True)          # per-jump mass limit in kg (dogma 1385)


class SDEMoon(Base):
    """mapMoons — moon data for counting moons per planet."""
    __tablename__ = "sde_moons"

    moon_id = Column(Integer, primary_key=True)
    planet_id = Column(Integer, nullable=False, index=True)
    system_id = Column(Integer, nullable=False, index=True)


class SDEStar(Base):
    """mapStars — star data per solar system."""
    __tablename__ = "sde_stars"

    system_id = Column(Integer, primary_key=True)
    type_id = Column(Integer, nullable=True)
    star_name = Column(String, nullable=True)


# ── Dogma attribute tables (fitting tool) ─────────────────────────────────

class SDEDogmaAttribute(Base):
    """dogmaAttributes — attribute definitions (id, name, unit, etc.)."""
    __tablename__ = "sde_dogma_attributes"

    attribute_id = Column(Integer, primary_key=True)
    attribute_name = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    default_value = Column(Float, nullable=True)
    stackable = Column(Boolean, default=True)
    high_is_good = Column(Boolean, default=True)
    unit_id = Column(Integer, nullable=True)


class SDETypeDogmaAttribute(Base):
    """Per-type dogma attribute values from typeDogma."""
    __tablename__ = "sde_type_dogma_attrs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_id = Column(Integer, nullable=False, index=True)
    attribute_id = Column(Integer, nullable=False)
    value = Column(Float, nullable=False)


class SDEModuleSlot(Base):
    """Pre-computed module slot type derived from dogma effects."""
    __tablename__ = "sde_module_slots"

    type_id = Column(Integer, primary_key=True)
    slot_type = Column(String, nullable=False)
    is_turret = Column(Boolean, default=False)
    is_launcher = Column(Boolean, default=False)


class SDEMarketGroup(Base):
    """marketGroups — hierarchical market categories for browsing modules."""
    __tablename__ = "sde_market_groups"

    market_group_id = Column(Integer, primary_key=True)
    parent_group_id = Column(Integer, nullable=True, index=True)
    market_group_name = Column(String, nullable=False)
    icon_id = Column(Integer, nullable=True)


class SDETypeBonus(Base):
    """typeBonus — ship hull bonuses parsed from CCP traits data."""
    __tablename__ = "sde_type_bonuses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_id = Column(Integer, nullable=False, index=True)
    bonus_value = Column(Float, nullable=False)
    is_role_bonus = Column(Boolean, default=False)
    scaling_skill_id = Column(Integer, nullable=True)
    target_type_id = Column(Integer, nullable=True)
    bonus_keyword = Column(String, nullable=True)


# ── Dogma effect tables (fitting engine modifier pipeline) ───────────────

class SDEEffect(Base):
    """dogmaEffects — effect definitions with category and modifier info."""
    __tablename__ = "sde_effects"

    effect_id = Column(Integer, primary_key=True)
    effect_name = Column(String, nullable=False)
    effect_category = Column(Integer, nullable=False, default=0)
    discharge_attribute_id = Column(Integer, nullable=True)
    duration_attribute_id = Column(Integer, nullable=True)


class SDETypeEffect(Base):
    """Per-type effect assignments from typeDogma."""
    __tablename__ = "sde_type_effects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_id = Column(Integer, nullable=False, index=True)
    effect_id = Column(Integer, nullable=False, index=True)
    is_default = Column(Boolean, default=False)


class SDEModifier(Base):
    """Parsed modifier info from dogma effects — what each effect actually does."""
    __tablename__ = "sde_modifiers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    effect_id = Column(Integer, nullable=False, index=True)
    func = Column(String, nullable=False)
    domain = Column(String, nullable=False)
    modified_attribute_id = Column(Integer, nullable=False, index=True)
    modifying_attribute_id = Column(Integer, nullable=False)
    operator = Column(Integer, nullable=False)
    filter_type = Column(String, nullable=True)
    filter_value = Column(Integer, nullable=True)
