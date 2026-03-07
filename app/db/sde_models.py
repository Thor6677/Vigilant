from sqlalchemy import Column, Integer, String, Float, Boolean, Text
from app.db.models import Base


class SDEType(Base):
    """invTypes — item type_id <-> name mapping."""
    __tablename__ = "sde_types"

    type_id = Column(Integer, primary_key=True)
    type_name = Column(String, nullable=False, index=True)
    group_id = Column(Integer, nullable=True)
    category_id = Column(Integer, nullable=True)
    published = Column(Boolean, default=True)


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


class SDEMeta(Base):
    """Tracks SDE version and last update time."""
    __tablename__ = "sde_meta"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
