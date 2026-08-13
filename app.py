# app.py - PM2.5 Wildfire Dashboard
# By: Hassan Khan

# Standard library imports
import logging
import warnings
import os
import math
import csv
from pathlib import Path
from datetime import datetime, date
from contextlib import asynccontextmanager
from typing import Optional, Literal
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import time

# Third-party imports
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Depends, status, BackgroundTasks, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, and_, extract
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool
from iptocc import country_code
import geopandas as gpd
from shapely.geometry import mapping
import gzip
import json

# Local application imports
from db.database import SessionLocal
from db.models import (
    DailyPM25, County, Population,
    YearlyPM25Summary, MonthlyPM25Summary, SeasonalPM25Summary,
    ExcessMortalitySummary, ExceedanceSummary, DecompositionSummary,
    PageView
)

# Suppress warnings from GeoPandas
warnings.filterwarnings(
    'ignore', message='.*initial implementation of Parquet.*')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Cache configuration
CACHE_TTL = 3600  # 1 hour (data rarely changes)
executor = ThreadPoolExecutor(max_workers=4)

# Load county geometries once at startup
COUNTY_GEOMETRIES = None

# Simple in-memory cache for choropleth data
CHOROPLETH_CACHE = {}
CACHE_MAX_SIZE = 500  # Maximum number of cached responses (increased from 100)


def load_county_geometries():
    """Load county geometries from the shapefile and cache them."""
    global COUNTY_GEOMETRIES
    if COUNTY_GEOMETRIES is None:
        try:
            # Path to the county boundaries shapefile
            shapefile_path = "data/shapefiles/county/cb_2018_us_county_5m.shp"

            # Read the shapefile
            gdf = gpd.read_file(shapefile_path)

            # Create a FIPS code column by combining state and county codes
            gdf['FIPS'] = gdf['STATEFP'] + gdf['COUNTYFP']

            # Convert to GeoJSON and index by FIPS code
            COUNTY_GEOMETRIES = {}
            for _, row in gdf.iterrows():
                feature = {
                    'type': 'Feature',
                    'geometry': mapping(row['geometry']),
                    'properties': {
                        'FIPS': row['FIPS'],
                        'NAME': row.get('NAME', ''),
                        'STATEFP': row.get('STATEFP', ''),
                        'COUNTYFP': row.get('COUNTYFP', '')
                    }
                }
                COUNTY_GEOMETRIES[row['FIPS']] = feature['geometry']

            logger.info("Loaded %d county geometries", len(COUNTY_GEOMETRIES))

        except Exception as e:
            logger.error("Error loading county geometries: %s",
                         str(e), exc_info=True)
            COUNTY_GEOMETRIES = {}

    return COUNTY_GEOMETRIES


def get_cache_key(endpoint: str, **params) -> str:
    """Generate a cache key for the given endpoint and parameters."""
    # Sort parameters to ensure consistent cache keys
    sorted_params = sorted(params.items())
    param_str = "&".join(
        [f"{k}={v}" for k, v in sorted_params if v is not None])
    return f"{endpoint}:{param_str}"


def get_from_cache(cache_key: str):
    """Get data from cache if it exists and hasn't expired."""
    if cache_key in CHOROPLETH_CACHE:
        data, timestamp = CHOROPLETH_CACHE[cache_key]
        # Check if cache entry is still valid (5 minutes)
        if (datetime.now().timestamp() - timestamp) < CACHE_TTL:
            return data
        else:
            # Remove expired entry
            del CHOROPLETH_CACHE[cache_key]
    return None


def set_cache(cache_key: str, data):
    """Store data in cache with timestamp."""
    # Simple LRU: remove oldest entries if cache is full
    if len(CHOROPLETH_CACHE) >= CACHE_MAX_SIZE:
        # Remove the oldest entry
        oldest_key = min(CHOROPLETH_CACHE.keys(),
                         key=lambda k: CHOROPLETH_CACHE[k][1])
        del CHOROPLETH_CACHE[oldest_key]

    CHOROPLETH_CACHE[cache_key] = (data, datetime.now().timestamp())


async def preload_common_datasets():
    """Preload common choropleth datasets to warm up the cache."""

    # Common datasets to preload
    common_requests = [
        # Most common PM2.5 average requests
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2020, "sub_metric": "total"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2021, "sub_metric": "total"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2022, "sub_metric": "total"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2023, "sub_metric": "total"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2020, "sub_metric": "fire"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2021, "sub_metric": "fire"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2022, "sub_metric": "fire"},
        {"endpoint": "average", "time_scale": "yearly",
            "year": 2023, "sub_metric": "fire"},

        # Common max PM2.5 requests
        {"endpoint": "max", "time_scale": "yearly",
            "year": 2020, "sub_metric": "total"},
        {"endpoint": "max", "time_scale": "yearly",
            "year": 2021, "sub_metric": "total"},
        {"endpoint": "max", "time_scale": "yearly",
            "year": 2022, "sub_metric": "total"},
        {"endpoint": "max", "time_scale": "yearly",
            "year": 2023, "sub_metric": "total"},

        # Common population requests
        {"endpoint": "population", "year": 2020},
        {"endpoint": "population", "year": 2021},
        {"endpoint": "population", "year": 2022},
        {"endpoint": "population", "year": 2023},
    ]

    # Preload each dataset
    for request_params in common_requests:
        try:
            cache_key = get_cache_key(**request_params)

            # Skip if already cached
            if get_from_cache(cache_key):
                continue

            # Create a database session for preloading
            db = SessionLocal()
            try:
                if request_params["endpoint"] == "average":
                    summary_model = YearlyPM25Summary
                    query = build_choropleth_query(
                        db, request_params["time_scale"],
                        request_params["year"], None, None, summary_model)
                    results = query.all()
                    col_map = {"total": "avg_total",
                               "fire": "avg_fire", "nonfire": "avg_nonfire"}
                    col_name = col_map.get(
                        request_params["sub_metric"], "avg_total")

                    def value_func(row):
                        return float(getattr(row, col_name) or 0)
                    features = build_geojson_features(results, value_func)

                    response_data = {
                        "type": "FeatureCollection",
                        "features": features,
                        "metadata": {
                            "time_scale": request_params["time_scale"],
                            "year": request_params["year"],
                            "metric": col_name,
                            "feature_count": len(features)
                        }
                    }

                elif request_params["endpoint"] == "max":
                    summary_model = YearlyPM25Summary
                    query = build_choropleth_query(
                        db, request_params["time_scale"],
                        request_params["year"], None, None, summary_model)
                    results = query.all()
                    col_map = {"total": "max_total",
                               "fire": "max_fire", "nonfire": "max_nonfire"}
                    col_name = col_map.get(
                        request_params["sub_metric"], "max_total")

                    def value_func(row):
                        return float(getattr(row, col_name) or 0)
                    features = build_geojson_features(results, value_func)

                    response_data = {
                        "type": "FeatureCollection",
                        "features": features,
                        "metadata": {
                            "time_scale": request_params["time_scale"],
                            "year": request_params["year"],
                            "metric": col_name,
                            "feature_count": len(features)
                        }
                    }

                elif request_params["endpoint"] == "population":
                    query = db.query(
                        County.fips,
                        County.name.label("county_name"),
                        County.geometry,
                        Population.population
                    ).outerjoin(
                        Population, and_(
                            Population.fips == County.fips,
                            Population.year == request_params["year"],
                            Population.age_group == 0
                        )
                    ).filter(~County.fips.startswith('72'))

                    results = query.all()
                    features = []

                    for row in results:
                        if not row.geometry:
                            continue
                        pop = row.population or 0
                        if pop is None or not math.isfinite(pop):
                            pop = 0
                        feature = {
                            "type": "Feature",
                            "geometry": row.geometry,
                            "properties": {
                                "fips": row.fips,
                                "county_name": row.county_name,
                                "value": int(pop),
                                "population": int(pop)
                            }
                        }
                        features.append(feature)

                    response_data = {
                        "type": "FeatureCollection",
                        "features": features,
                        "metadata": {
                            "time_scale": "yearly",
                            "year": request_params["year"],
                            "metric": "population",
                            "feature_count": len(features)
                        }
                    }

                # Cache the response
                set_cache(cache_key, response_data)

            finally:
                db.close()

        except Exception as e:
            continue


# Pydantic models for request validation
class DownloadRequest(BaseModel):
    name: str
    institution: str
    email: str
    usage_description: str
    data_type: str  # 'pm25', 'mortality', or 'yll'
    time_scale: Optional[str] = 'yearly'  # 'daily', 'yearly', 'monthly', 'seasonal'
    start_year: int
    end_year: int
    counties: Optional[str] = None  # Comma-separated FIPS codes or 'all'
    states: Optional[str] = None  # Comma-separated state names or 'all'
    age_groups: Optional[str] = None  # For mortality/yll only


VALID_PAGEVIEW_PATHS = {
    "/map": "Map",
    "/about": "About",
    "/partners": "Partners",
    "/methodology": "Data & Methodology",
}


class PageViewRequest(BaseModel):
    path: Literal["/map", "/about", "/partners", "/methodology"]


# Application lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize resources
    logger.info("Starting up...")

    # Load county geometries
    load_county_geometries()

    # Preload common choropleth datasets
    await preload_common_datasets()

    yield

    # Shutdown: Clean up resources
    logger.info("Shutting down...")
    executor.shutdown(wait=True)

# Initialize FastAPI app with lifespan
app = FastAPI(
    title="PM2.5 Wildfire Dashboard",
    description="API for accessing PM2.5 wildfire data with preprocessed summaries",
    version="2.0.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Add GZip compression middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)


# Database session dependency
def get_db():
    """Dependency that provides a DB session."""
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error("Database error: %s", str(e))
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred"
        )
    finally:
        db.close()


def _get_client_ip(request: Request) -> str:
    """Resolve the real client IP. X-Real-IP is set unconditionally by the
    Apache edge proxy from its own REMOTE_ADDR (see apache-config.conf) and
    passed through untouched by nginx, so it's trustworthy end-to-end for
    both rate limiting and country lookups. Fall back to legacy
    X-Forwarded-For parsing only for direct/local access that bypasses that
    proxy chain (e.g. local dev)."""
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()


# Helper functions


def get_season_date_range(year: int, season: str):
    season = season.lower()
    if season == "winter":
        start_date = date(year-1, 12, 21)
        end_date = date(year, 3, 20)
    elif season == "spring":
        start_date = date(year, 3, 21)
        end_date = date(year, 6, 20)
    elif season == "summer":
        start_date = date(year, 6, 21)
        end_date = date(year, 9, 20)
    elif season == "fall":
        start_date = date(year, 9, 21)
        end_date = date(year, 12, 20)
    else:
        raise ValueError("Invalid season")
    return start_date, end_date


def parse_flexible_date(date_str: str) -> date:
    """Parse a date string into a date object with flexible format."""
    try:
        if len(date_str) == 4:  # Just year (e.g., '2023')
            return datetime.strptime(f"{date_str}-01-01", "%Y-%m-%d").date()
        elif len(date_str) == 7:  # Year and month (e.g., '2023-01')
            return datetime.strptime(f"{date_str}-01", "%Y-%m-%d").date()
        else:  # Full date (e.g., '2023-01-01')
            return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format: {date_str}. Use YYYY, YYYY-MM, or YYYY-MM-DD."
        ) from e


def get_season_from_date(date_obj: date) -> str:
    """Determine season from a date."""
    month = date_obj.month
    day = date_obj.day

    if (month == 12 and day >= 21) or month in [1, 2] or (month == 3 and day < 21):
        return "winter"
    elif (month == 3 and day >= 21) or month in [4, 5] or (month == 6 and day < 21):
        return "spring"
    elif (month == 6 and day >= 21) or month in [7, 8] or (month == 9 and day < 21):
        return "summer"
    else:
        return "fall"

# --- Helper Functions for Choropleth Endpoints ---


def build_choropleth_query(db, time_scale, year, month, season, summary_model):
    if time_scale == "yearly":
        if not year:
            raise HTTPException(
                status_code=400, detail="Year required for yearly data")
        query = db.query(
            summary_model.fips,
            County.name.label("county_name"),
            County.geometry,
            summary_model.avg_total,
            summary_model.avg_fire,
            summary_model.avg_nonfire,
            summary_model.max_total,
            summary_model.max_fire,
            summary_model.max_nonfire,
            summary_model.pop_weighted_total,
            summary_model.pop_weighted_fire,
            summary_model.pop_weighted_nonfire,
            Population.population
        ).join(
            County, County.fips == summary_model.fips
        ).outerjoin(
            Population, and_(
                Population.fips == summary_model.fips,
                Population.year == year,
                Population.age_group == 0
            )
        ).filter(
            summary_model.year == year,
            ~County.fips.startswith('72')
        )
    elif time_scale == "monthly":
        if not year or not month:
            raise HTTPException(
                status_code=400, detail="Year and month required for monthly data")
        query = db.query(
            summary_model.fips,
            County.name.label("county_name"),
            County.geometry,
            summary_model.avg_total,
            summary_model.avg_fire,
            summary_model.avg_nonfire,
            summary_model.max_total,
            summary_model.max_fire,
            summary_model.max_nonfire,
            summary_model.pop_weighted_total,
            summary_model.pop_weighted_fire,
            summary_model.pop_weighted_nonfire,
            Population.population
        ).join(
            County, County.fips == summary_model.fips
        ).outerjoin(
            Population, and_(
                Population.fips == summary_model.fips,
                Population.year == year,
                Population.age_group == 0
            )
        ).filter(
            summary_model.year == year,
            summary_model.month == month,
            ~County.fips.startswith('72')
        )
    elif time_scale == "seasonal":
        if not year or not season:
            raise HTTPException(
                status_code=400, detail="Year and season required for seasonal data")
        query = db.query(
            summary_model.fips,
            County.name.label("county_name"),
            County.geometry,
            summary_model.avg_total,
            summary_model.avg_fire,
            summary_model.avg_nonfire,
            summary_model.max_total,
            summary_model.max_fire,
            summary_model.max_nonfire,
            summary_model.pop_weighted_total,
            summary_model.pop_weighted_fire,
            summary_model.pop_weighted_nonfire,
            Population.population
        ).join(
            County, County.fips == summary_model.fips
        ).outerjoin(
            Population, and_(
                Population.fips == summary_model.fips,
                Population.year == year,
                Population.age_group == 0
            )
        ).filter(
            summary_model.year == year,
            summary_model.season == season.lower(),
            ~County.fips.startswith('72')
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid time_scale")
    return query


def build_geojson_features(results, value_func, include_geometry=False):
    """
    Build GeoJSON features from query results.

    Args:
        results: Query results
        value_func: Function to extract the value from a row
        include_geometry: If True, include geometry in the response. Default False for performance.
    """
    features = []
    for row in results:
        feature = {
            "type": "Feature",
            "properties": {
                "fips": row.fips,
                "county_name": row.county_name,
                "value": float(value_func(row)),
                "avg_total": float(row.avg_total) if row.avg_total is not None else 0,
                "avg_fire": float(row.avg_fire) if row.avg_fire is not None else 0,
                "avg_nonfire": float(row.avg_nonfire) if row.avg_nonfire is not None else 0,
                "max_total": float(row.max_total) if row.max_total is not None else 0,
                "max_fire": float(row.max_fire) if row.max_fire is not None else 0,
                "max_nonfire": float(row.max_nonfire) if row.max_nonfire is not None else 0,
                "pop_weighted_total": float(row.pop_weighted_total) if row.pop_weighted_total is not None else 0,
                "pop_weighted_fire": float(row.pop_weighted_fire) if row.pop_weighted_fire is not None else 0,
                "pop_weighted_nonfire": float(row.pop_weighted_nonfire) if row.pop_weighted_nonfire is not None else 0,
                "population": int(row.population) if row.population is not None else 0
            }
        }
        # Only include geometry if explicitly requested
        if include_geometry and row.geometry:
            feature["geometry"] = row.geometry
        features.append(feature)
    return features


# --- New Modular Endpoints ---
choropleth_router = APIRouter(
    prefix="/api/counties/choropleth", tags=["Choropleth"])


@choropleth_router.get("/geometries")
async def get_county_geometries():
    """
    Return county geometries for client-side caching.
    This should be called once on app load and cached on the frontend.
    Returns a GeoJSON FeatureCollection with just FIPS and geometry.
    """
    if not COUNTY_GEOMETRIES:
        raise HTTPException(
            status_code=500, detail="County geometries not loaded")

    features = []
    for fips, geometry in COUNTY_GEOMETRIES.items():
        # Skip Alaska and Puerto Rico
        if fips.startswith('02') or fips.startswith('72'):
            continue
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "fips": fips
            }
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "description": "County geometries for client-side rendering",
            "feature_count": len(features)
        }
    }


# Add a helper to sanitize float values for JSON


def safe_float(val):
    """Convert value to float, handling None, NaN, and infinite cases"""
    if val is None:
        return 0.0
    try:
        result = float(val)
        # Check if result is NaN or infinite
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except (ValueError, TypeError):
        return 0.0


@choropleth_router.get("/mortality")
async def get_choropleth_mortality(
    year: int = Query(..., description="Year required for mortality data"),
    sub_metric: str = Query("total", pattern="^(total|fire|nonfire)$",
                            description="Which excess mortality to return: total, fire, or nonfire"),
    age_group: Optional[str] = Query(
        None, description="Comma-separated age group indices (e.g., '1,2,3') (optional, 1-18)"),
    db: Session = Depends(get_db)
):
    """Get mortality impact choropleth data (precomputed summary, total/fire/nonfire), optionally by age group(s)."""
    try:
        base_query = db.query(
            County.fips,
            County.name.label("county_name"),
            County.geometry,
            ExcessMortalitySummary.total_excess,
            ExcessMortalitySummary.fire_excess,
            ExcessMortalitySummary.nonfire_excess,
            ExcessMortalitySummary.population,
            ExcessMortalitySummary.age_group
        ).join(
            ExcessMortalitySummary, and_(
                ExcessMortalitySummary.fips == County.fips,
                ExcessMortalitySummary.year == year
            )
        ).filter(
            ~County.fips.startswith('72')
        )
        age_group_list = None
        if age_group is not None and age_group.strip() != '':
            age_group_list = [int(x) for x in age_group.split(
                ',') if x.strip().isdigit()]
        if age_group_list:
            # Sum over selected age groups for each county
            subq = db.query(
                ExcessMortalitySummary.fips,
                func.sum(ExcessMortalitySummary.total_excess).label(
                    "total_excess"),
                func.sum(ExcessMortalitySummary.fire_excess).label(
                    "fire_excess"),
                func.sum(ExcessMortalitySummary.nonfire_excess).label(
                    "nonfire_excess"),
                func.sum(ExcessMortalitySummary.population).label("population")
            ).filter(
                ExcessMortalitySummary.year == year,
                ExcessMortalitySummary.age_group.in_(age_group_list)
            ).group_by(ExcessMortalitySummary.fips).subquery()
            results = db.query(
                County.fips,
                County.name.label("county_name"),
                County.geometry,
                subq.c.total_excess,
                subq.c.fire_excess,
                subq.c.nonfire_excess,
                subq.c.population
            ).join(subq, subq.c.fips == County.fips).filter(~County.fips.startswith('72')).all()
        else:
            # Sum over all age groups for each county-year
            subq = db.query(
                ExcessMortalitySummary.fips,
                func.sum(ExcessMortalitySummary.total_excess).label(
                    "total_excess"),
                func.sum(ExcessMortalitySummary.fire_excess).label(
                    "fire_excess"),
                func.sum(ExcessMortalitySummary.nonfire_excess).label(
                    "nonfire_excess"),
                func.sum(ExcessMortalitySummary.population).label("population")
            ).filter(
                ExcessMortalitySummary.year == year
            ).group_by(ExcessMortalitySummary.fips).subquery()
            results = db.query(
                County.fips,
                County.name.label("county_name"),
                County.geometry,
                subq.c.total_excess,
                subq.c.fire_excess,
                subq.c.nonfire_excess,
                subq.c.population
            ).join(subq, subq.c.fips == County.fips).filter(~County.fips.startswith('72')).all()

        def safe_float(value):
            """Convert value to float, handling None and NaN cases"""
            if value is None:
                return 0.0
            try:
                result = float(value)
                # Check if result is NaN or infinite
                if math.isnan(result) or math.isinf(result):
                    return 0.0
                return result
            except (ValueError, TypeError):
                return 0.0

        features = []
        for row in results:
            # Get the excess value based on sub_metric, with safe conversion
            if sub_metric == "total":
                excess = safe_float(row.total_excess)
            elif sub_metric == "fire":
                excess = safe_float(row.fire_excess)
            elif sub_metric == "nonfire":
                excess = safe_float(row.nonfire_excess)
            else:
                excess = safe_float(row.total_excess)

            population = row.population or 0

            # Safe calculation of the rate
            if population > 0 and excess is not None:
                value = (excess / population) * 100
                # Double-check the result isn't NaN
                if math.isnan(value) or math.isinf(value):
                    value = 0.0
            else:
                value = 0.0

            feature = {
                "type": "Feature",
                "properties": {
                    "fips": row.fips,
                    "county_name": row.county_name,
                    "value": value,
                    "total_excess": safe_float(row.total_excess),
                    "fire_excess": safe_float(row.fire_excess),
                    "nonfire_excess": safe_float(row.nonfire_excess),
                    "population": int(row.population) if row.population is not None else 0
                }
            }
            features.append(feature)

        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "time_scale": "yearly",
                "year": year,
                "metric": f"excess_mortality_{sub_metric}",
                "feature_count": len(features),
                "age_group": age_group
            }
        }
    except Exception as e:
        logger.error("Error in mortality choropleth: %s",
                     str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@choropleth_router.get("/average")
async def get_choropleth_average(
    time_scale: str = Query("yearly", pattern="^(yearly|monthly|seasonal)$"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    season: Optional[str] = Query(None),
    sub_metric: str = Query("total", pattern="^(total|fire|nonfire)$"),
    db: Session = Depends(get_db)
):
    """Get average PM2.5 choropleth data."""
    # Check cache first
    cache_key = get_cache_key("average",
                              time_scale=time_scale,
                              year=year,
                              month=month,
                              season=season,
                              sub_metric=sub_metric)

    cached_data = get_from_cache(cache_key)
    if cached_data:
        return cached_data

    summary_model = {
        "yearly": YearlyPM25Summary,
        "monthly": MonthlyPM25Summary,
        "seasonal": SeasonalPM25Summary
    }[time_scale]
    query = build_choropleth_query(
        db, time_scale, year, month, season, summary_model)
    results = query.all()
    col_map = {
        "total": "avg_total",
        "fire": "avg_fire",
        "nonfire": "avg_nonfire"
    }
    col_name = col_map.get(sub_metric, "avg_total")

    def value_func(row):
        return float(getattr(row, col_name) or 0)
    features = build_geojson_features(results, value_func)

    response_data = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "time_scale": time_scale,
            "year": year,
            "month": month,
            "season": season,
            "metric": col_name,
            "feature_count": len(features)
        }
    }

    # Cache the response
    set_cache(cache_key, response_data)

    return response_data


@choropleth_router.get("/max")
async def get_choropleth_max(
    time_scale: str = Query("yearly", pattern="^(yearly|monthly|seasonal)$"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    season: Optional[str] = Query(None),
    sub_metric: str = Query("total", pattern="^(total|fire|nonfire)$"),
    db: Session = Depends(get_db)
):
    """Get max PM2.5 choropleth data."""
    # Check cache first
    cache_key = get_cache_key("max",
                              time_scale=time_scale,
                              year=year,
                              month=month,
                              season=season,
                              sub_metric=sub_metric)

    cached_data = get_from_cache(cache_key)
    if cached_data:
        return cached_data

    summary_model = {
        "yearly": YearlyPM25Summary,
        "monthly": MonthlyPM25Summary,
        "seasonal": SeasonalPM25Summary
    }[time_scale]
    query = build_choropleth_query(
        db, time_scale, year, month, season, summary_model)
    results = query.all()
    col_map = {
        "total": "max_total",
        "fire": "max_fire",
        "nonfire": "max_nonfire"
    }
    col_name = col_map.get(sub_metric, "max_total")

    def value_func(row):
        return float(getattr(row, col_name) or 0)
    features = build_geojson_features(results, value_func)

    response_data = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "time_scale": time_scale,
            "year": year,
            "month": month,
            "season": season,
            "metric": col_name,
            "feature_count": len(features)
        }
    }

    # Cache the response
    set_cache(cache_key, response_data)

    return response_data


@choropleth_router.get("/pop_weighted")
async def get_choropleth_pop_weighted(
    time_scale: str = Query("yearly", pattern="^(yearly|monthly|seasonal)$"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    season: Optional[str] = Query(None),
    sub_metric: str = Query("total", pattern="^(total|fire|nonfire)$"),
    db: Session = Depends(get_db)
):
    """Get population-weighted PM2.5 choropleth data."""
    summary_model = {
        "yearly": YearlyPM25Summary,
        "monthly": MonthlyPM25Summary,
        "seasonal": SeasonalPM25Summary
    }[time_scale]
    query = build_choropleth_query(
        db, time_scale, year, month, season, summary_model)
    results = query.all()
    col_map = {
        "total": "pop_weighted_total",
        "fire": "pop_weighted_fire",
        "nonfire": "pop_weighted_nonfire"
    }
    col_name = col_map.get(sub_metric, "pop_weighted_total")

    def value_func(row):
        return float(getattr(row, col_name) or 0)
    features = build_geojson_features(results, value_func)
    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "time_scale": time_scale,
            "year": year,
            "month": month,
            "season": season,
            "metric": col_name,
            "feature_count": len(features)
        }
    }


@choropleth_router.get("/population")
async def get_choropleth_population(
    year: Optional[int] = Query(
        2020, description="Year for population data (optional, defaults to 2020)"),
    db: Session = Depends(get_db)
):
    """Get population choropleth data."""
    try:
        # Build query for population data
        query = db.query(
            County.fips,
            County.name.label("county_name"),
            County.geometry,
            Population.population
        ).outerjoin(  # Use LEFT JOIN to include counties even if they don't have population data
            Population, and_(
                Population.fips == County.fips,
                Population.year == year,
                Population.age_group == 0  # Only total population
            )
        ).filter(
            ~County.fips.startswith('72')  # Exclude Puerto Rico
        )

        results = query.all()
        features = []

        for row in results:
            pop = row.population or 0

            # Ensure valid number
            if pop is None or not math.isfinite(pop):
                pop = 0

            feature = {
                "type": "Feature",
                "properties": {
                    "fips": row.fips,
                    "county_name": row.county_name,
                    "value": int(pop),
                    "population": int(pop)
                }
            }
            features.append(feature)

        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "time_scale": "yearly",
                "year": year,
                "metric": "population",
                "feature_count": len(features)
            }
        }

    except Exception as e:
        logger.error("Error in population choropleth: %s",
                     str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@choropleth_router.get("/yll")
async def get_choropleth_yll(
    year: int = Query(..., description="Year required for YLL data"),
    sub_metric: str = Query("total", pattern="^(total|fire|nonfire)$",
                            description="Which YLL to return: total, fire, or nonfire"),
    age_group: Optional[str] = Query(
        None, description="Comma-separated age group indices (e.g., '1,2,3') (optional, 1-18)"),
    db: Session = Depends(get_db)
):
    """Get YLL impact choropleth data (precomputed summary, total/fire/nonfire), optionally by age group(s)."""

    try:
        age_group_list = None
        if age_group is not None and age_group.strip() != '':
            age_group_list = [int(x) for x in age_group.split(
                ',') if x.strip().isdigit()]

        if age_group_list:
            # Sum over selected age groups for each county
            subq = db.query(
                ExcessMortalitySummary.fips,
                func.sum(ExcessMortalitySummary.yll_total).label("yll_total"),
                func.sum(ExcessMortalitySummary.yll_fire).label("yll_fire"),
                func.sum(ExcessMortalitySummary.yll_nonfire).label(
                    "yll_nonfire"),
                func.sum(ExcessMortalitySummary.population).label("population")
            ).filter(
                ExcessMortalitySummary.year == year,
                ExcessMortalitySummary.age_group.in_(age_group_list)
            ).group_by(ExcessMortalitySummary.fips).subquery()
        else:
            # Sum over all age groups for each county-year
            subq = db.query(
                ExcessMortalitySummary.fips,
                func.sum(ExcessMortalitySummary.yll_total).label("yll_total"),
                func.sum(ExcessMortalitySummary.yll_fire).label("yll_fire"),
                func.sum(ExcessMortalitySummary.yll_nonfire).label(
                    "yll_nonfire"),
                func.sum(ExcessMortalitySummary.population).label("population")
            ).filter(
                ExcessMortalitySummary.year == year
            ).group_by(ExcessMortalitySummary.fips).subquery()

        results = db.query(
            County.fips,
            County.name.label("county_name"),
            County.geometry,
            subq.c.yll_total,
            subq.c.yll_fire,
            subq.c.yll_nonfire,
            subq.c.population
        ).join(subq, subq.c.fips == County.fips).filter(~County.fips.startswith('72')).all()

        features = []
        for row in results:
            # Get the YLL value based on sub_metric, with safe conversion
            if sub_metric == "total":
                yll_value = safe_float(row.yll_total)
            elif sub_metric == "fire":
                yll_value = safe_float(row.yll_fire)
            elif sub_metric == "nonfire":
                yll_value = safe_float(row.yll_nonfire)
            else:
                yll_value = 0.0

            total_pop = safe_float(row.population)

            # Normalize YLL: yll_value / total_pop
            normalized_yll = safe_float(
                yll_value / total_pop if total_pop > 0 else 0.0)

            # Debug logging for problematic values
            if math.isnan(normalized_yll) or math.isinf(normalized_yll):
                logger.warning(
                    f"NaN/Inf detected for county {row.fips}: yll_value={yll_value}, total_pop={total_pop}, normalized_yll={normalized_yll}")

            feature = {
                "type": "Feature",
                "properties": {
                    "fips": row.fips,
                    "county_name": row.county_name,
                    "yll_total": safe_float(row.yll_total),
                    "yll_fire": safe_float(row.yll_fire),
                    "yll_nonfire": safe_float(row.yll_nonfire),
                    "population": int(safe_float(total_pop)),
                    "value": normalized_yll
                }
            }
            features.append(feature)

        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "time_scale": "yearly",
                "year": year,
                "metric": f"yll_{sub_metric}",
                "feature_count": len(features),
                "age_group": age_group,
                "value_description": "normalized_yll_fraction"
            }
        }
    except Exception as e:
        logger.error("Error in YLL choropleth: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# Register the router
app.include_router(choropleth_router)


@app.get("/api/counties/decomp/{fips}")
def get_county_decomposition_info(
    fips: str,
    pm25_type: str = "total",
    db: Session = Depends(get_db)
):
    """
    Get decomposition summary and county info for a given county FIPS.
    Returns the latest available decomposition result.

    Args:
        fips (str): County FIPS code
        pm25_type (str): Type of PM2.5 analysis - 'total' or 'fire' (default: 'total')
    """
    # Validate pm25_type parameter
    if pm25_type not in ["total", "fire"]:
        raise HTTPException(
            status_code=400,
            detail="pm25_type must be 'total' or 'fire'"
        )

    county = db.query(County).filter(County.fips == fips).first()
    if not county:
        raise HTTPException(status_code=404, detail="County not found")

    # Map PM2.5 type to age_group code (using our workaround)
    age_group_code = -1 if pm25_type == "total" else -2

    # Query using age_group code instead of None
    decomp = db.query(DecompositionSummary)\
        .filter(
            DecompositionSummary.fips == fips,
            DecompositionSummary.age_group == age_group_code
    )\
        .order_by(DecompositionSummary.end_year.desc())\
        .first()

    if not decomp:
        pm25_desc = "total PM2.5" if pm25_type == "total" else "fire PM2.5"
        raise HTTPException(
            status_code=404,
            detail=f"Decomposition summary not found for this county ({pm25_desc})"
        )

    return {
        "fips": county.fips,
        "county_name": county.name,
        "pm25_type": pm25_type,
        "start_year": decomp.start_year,
        "end_year": decomp.end_year,
        "decomposition": {
            "population_growth": decomp.population_growth,
            "population_ageing": decomp.population_ageing,
            "baseline_mortality_change": decomp.baseline_mortality_change,
            "exposure_change": decomp.exposure_change,
            "total_change": decomp.total_change
        }
    }


@app.get("/api/pm25/bar_chart/{fips}")
async def get_bar_chart_data(
    fips: str,
    time_scale: str = Query(
        "yearly", pattern="^(yearly|monthly|seasonal|daily)$"),
    start_year: Optional[int] = Query(None),
    end_year: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    season: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Get preprocessed bar chart data for a specific county.
    Uses summary tables when possible for better performance.

    Returns a list of data points with the following structure:
    [
      {
        "year": int,               # Year of the data point
        "month": Optional[int],    # Month (1-12) if time_scale is monthly
        "season": Optional[str],   # Season name if time_scale is seasonal
        "date": Optional[str],     # ISO date string if time_scale is daily
        "total": float,           # Total PM2.5 value
        "fire": float,            # Fire-related PM2.5
        "nonfire": float          # Non-fire PM2.5
      },
      ...
    ]
    """
    try:
        # Validate county exists
        county = db.query(County).filter(County.fips == fips).first()
        if not county:
            raise HTTPException(
                status_code=404, detail=f"County with FIPS {fips} not found")

        if time_scale == "yearly":
            query = db.query(YearlyPM25Summary).filter(
                YearlyPM25Summary.fips == fips)

            if start_year and end_year:
                query = query.filter(
                    YearlyPM25Summary.year >= start_year,
                    YearlyPM25Summary.year <= end_year
                )
            elif year:
                query = query.filter(YearlyPM25Summary.year == year)

            results = query.order_by(YearlyPM25Summary.year).all()

            data = [
                {
                    "year": r.year,
                    "total": float(r.avg_total),
                    "fire": float(r.avg_fire),
                    "nonfire": float(r.avg_nonfire),
                    "max_total": float(r.max_total),
                    "max_fire": float(r.max_fire),
                    "days_count": r.days_count
                }
                for r in results
            ]

        elif time_scale == "monthly":
            if not year:
                raise HTTPException(
                    status_code=400, detail="Year required for monthly data")

            results = db.query(MonthlyPM25Summary).filter(
                MonthlyPM25Summary.fips == fips,
                MonthlyPM25Summary.year == year
            ).order_by(MonthlyPM25Summary.month).all()

            data = [
                {
                    "year": r.year,
                    "month": r.month,
                    "total": float(r.avg_total),
                    "fire": float(r.avg_fire),
                    "nonfire": float(r.avg_nonfire),
                    "max_total": float(r.max_total),
                    "max_fire": float(r.max_fire),
                    "days_count": r.days_count
                }
                for r in results
            ]

        elif time_scale == "seasonal":
            query = db.query(SeasonalPM25Summary).filter(
                SeasonalPM25Summary.fips == fips)

            if start_year and end_year:
                query = query.filter(
                    SeasonalPM25Summary.year >= start_year,
                    SeasonalPM25Summary.year <= end_year
                )
            elif year:
                query = query.filter(SeasonalPM25Summary.year == year)

            results = query.order_by(
                SeasonalPM25Summary.year, SeasonalPM25Summary.season).all()

            data = [
                {
                    "year": r.year,
                    "season": r.season,
                    "total": float(r.avg_total),
                    "fire": float(r.avg_fire),
                    "nonfire": float(r.avg_nonfire),
                    "max_total": float(r.max_total),
                    "max_fire": float(r.max_fire),
                    "days_count": r.days_count
                }
                for r in results
            ]

        elif time_scale == "daily":
            # For daily data, use the original DailyPM25 table
            if not year:
                raise HTTPException(
                    status_code=400, detail="Year required for daily data")

            # Helper function to get season from month
            def get_season_from_month(month_num):
                if month_num in [12, 1, 2]:
                    return 'winter'
                elif month_num in [3, 4, 5]:
                    return 'spring'
                elif month_num in [6, 7, 8]:
                    return 'summer'
                else:  # 9, 10, 11
                    return 'fall'

            # Build the date range based on month or season
            if month:
                # Get daily data for specific month
                if month < 1 or month > 12:
                    raise HTTPException(
                        status_code=400, detail="Month must be between 1 and 12")

                # Calculate last day of month
                if month == 12:
                    next_month = 1
                    next_year = year + 1
                else:
                    next_month = month + 1
                    next_year = year

                start_date = date(year, month, 1)
                # First day of next month
                end_date = date(next_year, next_month, 1)

            elif season:
                # Get daily data for specific season
                season = season.lower()
                if season not in ['winter', 'spring', 'summer', 'fall']:
                    raise HTTPException(
                        status_code=400, detail="Season must be winter, spring, summer, or fall")

                if season == 'winter':
                    # Winter: Dec 21 - Mar 20
                    start_date = date(year - 1, 12, 21)
                    end_date = date(year, 3, 21)
                elif season == 'spring':
                    # Spring: Mar 21 - Jun 20
                    start_date = date(year, 3, 21)
                    end_date = date(year, 6, 21)
                elif season == 'summer':
                    # Summer: Jun 21 - Sep 20
                    start_date = date(year, 6, 21)
                    end_date = date(year, 9, 21)
                elif season == 'fall':
                    # Fall: Sep 21 - Dec 20
                    start_date = date(year, 9, 21)
                    end_date = date(year, 12, 21)
            else:
                # Get all daily data for the year
                start_date = date(year, 1, 1)
                end_date = date(year + 1, 1, 1)  # First day of next year

            results = db.query(DailyPM25).filter(
                DailyPM25.fips == fips,
                DailyPM25.date >= start_date,
                DailyPM25.date < end_date
            ).order_by(DailyPM25.date).all()

            data = [
                {
                    "date": r.date.isoformat(),
                    "year": r.date.year,
                    "month": r.date.month,
                    "day": r.date.day,
                    "total": float(r.total),
                    "fire": float(r.fire),
                    "nonfire": float(r.nonfire)
                }
                for r in results
            ]

        return data

    except Exception as e:
        logger.error("Error in bar chart data: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/counties/statistics")
async def get_county_statistics(
    time_scale: str = Query("yearly", pattern="^(yearly|monthly|seasonal)$"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    season: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Get statistical summaries across all counties."""
    try:
        if time_scale == "yearly":
            if not year:
                raise HTTPException(
                    status_code=400, detail="Year required for yearly statistics")

            stats = db.query(
                func.avg(YearlyPM25Summary.avg_total).label("mean_total"),
                func.avg(YearlyPM25Summary.avg_fire).label("mean_fire"),
                func.avg(YearlyPM25Summary.avg_nonfire).label("mean_nonfire"),
                func.percentile_cont(0.5).within_group(
                    YearlyPM25Summary.avg_total).label("median_total"),
                func.percentile_cont(0.5).within_group(
                    YearlyPM25Summary.avg_fire).label("median_fire"),
                func.min(YearlyPM25Summary.avg_total).label("min_total"),
                func.max(YearlyPM25Summary.avg_total).label("max_total"),
                func.count().label("county_count")
            ).filter(YearlyPM25Summary.year == year).first()

        elif time_scale == "monthly":
            if not year or not month:
                raise HTTPException(
                    status_code=400, detail="Year and month required for monthly statistics")

            stats = db.query(
                func.avg(MonthlyPM25Summary.avg_total).label("mean_total"),
                func.avg(MonthlyPM25Summary.avg_fire).label("mean_fire"),
                func.avg(MonthlyPM25Summary.avg_nonfire).label("mean_nonfire"),
                func.percentile_cont(0.5).within_group(
                    MonthlyPM25Summary.avg_total).label("median_total"),
                func.percentile_cont(0.5).within_group(
                    MonthlyPM25Summary.avg_fire).label("median_fire"),
                func.min(MonthlyPM25Summary.avg_total).label("min_total"),
                func.max(MonthlyPM25Summary.avg_total).label("max_total"),
                func.count().label("county_count")
            ).filter(
                MonthlyPM25Summary.year == year,
                MonthlyPM25Summary.month == month
            ).first()

        elif time_scale == "seasonal":
            if not year or not season:
                raise HTTPException(
                    status_code=400, detail="Year and season required for seasonal statistics")

            stats = db.query(
                func.avg(SeasonalPM25Summary.avg_total).label("mean_total"),
                func.avg(SeasonalPM25Summary.avg_fire).label("mean_fire"),
                func.avg(SeasonalPM25Summary.avg_nonfire).label(
                    "mean_nonfire"),
                func.percentile_cont(0.5).within_group(
                    SeasonalPM25Summary.avg_total).label("median_total"),
                func.percentile_cont(0.5).within_group(
                    SeasonalPM25Summary.avg_fire).label("median_fire"),
                func.min(SeasonalPM25Summary.avg_total).label("min_total"),
                func.max(SeasonalPM25Summary.avg_total).label("max_total"),
                func.count().label("county_count")
            ).filter(
                SeasonalPM25Summary.year == year,
                SeasonalPM25Summary.season == season.lower()
            ).first()

        return {
            "time_scale": time_scale,
            "year": year,
            "month": month,
            "season": season,
            "statistics": {
                "mean_total_pm25": float(stats.mean_total) if stats.mean_total else 0,
                "mean_fire_pm25": float(stats.mean_fire) if stats.mean_fire else 0,
                "mean_nonfire_pm25": float(stats.mean_nonfire) if stats.mean_nonfire else 0,
                "median_total_pm25": float(stats.median_total) if stats.median_total else 0,
                "median_fire_pm25": float(stats.median_fire) if stats.median_fire else 0,
                "min_total_pm25": float(stats.min_total) if stats.min_total else 0,
                "max_total_pm25": float(stats.max_total) if stats.max_total else 0,
                "county_count": int(stats.county_count) if stats.county_count else 0
            }
        }

    except Exception as e:
        logger.error("Error in statistics: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/excess_mortality")
def get_excess_mortality_summary(
    year: int = Query(None, description="Year to filter (optional)"),
    fips: str = Query(
        None, description="County FIPS code to filter (optional)"),
    sub_metric: str = Query("total", pattern="^(total|fire|nonfire)$",
                            description="Which excess mortality to return: total, fire, or nonfire"),
    age_group: Optional[str] = Query(
        None, description="Comma-separated age group indices (e.g., '1,2,3') (optional, 1-18)"),
    db: Session = Depends(get_db)
):
    """
    Return precomputed excess mortality summary (total, fire, nonfire) for each county-year-age_group. If age_group is not provided, return all age groups for the county-year.
    """
    try:
        age_group_list = None
        if age_group is not None and age_group.strip() != '':
            age_group_list = [int(x) for x in age_group.split(
                ',') if x.strip().isdigit()]
        # If fips is provided, group by year and sum over age groups
        if fips:
            query = db.query(
                ExcessMortalitySummary.year,
                func.sum(ExcessMortalitySummary.total_excess).label(
                    "total_excess"),
                func.sum(ExcessMortalitySummary.fire_excess).label(
                    "fire_excess"),
                func.sum(ExcessMortalitySummary.nonfire_excess).label(
                    "nonfire_excess"),
                func.sum(ExcessMortalitySummary.population).label("population")
            ).filter(ExcessMortalitySummary.fips == fips)
            if year:
                query = query.filter(ExcessMortalitySummary.year == year)
            if age_group_list:
                query = query.filter(
                    ExcessMortalitySummary.age_group.in_(age_group_list))
            query = query.group_by(ExcessMortalitySummary.year)
            results = []
            for row in query.all():
                if sub_metric == "total":
                    value = row.total_excess
                elif sub_metric == "fire":
                    value = row.fire_excess
                elif sub_metric == "nonfire":
                    value = row.nonfire_excess
                else:
                    value = row.total_excess
                results.append({
                    "year": row.year,
                    "population": int(row.population) if row.population is not None else 0,
                    "excess_mortality": float(value) if value is not None else 0.0,
                    "total_excess": float(row.total_excess) if row.total_excess is not None else 0.0,
                    "fire_excess": float(row.fire_excess) if row.fire_excess is not None else 0.0,
                    "nonfire_excess": float(row.nonfire_excess) if row.nonfire_excess is not None else 0.0
                })
            return results
        # Otherwise, return all age groups for the county-year (default behavior)
        query = db.query(ExcessMortalitySummary, County.name).join(
            County, County.fips == ExcessMortalitySummary.fips)
        if year:
            query = query.filter(ExcessMortalitySummary.year == year)
        if fips:
            query = query.filter(ExcessMortalitySummary.fips == fips)
        if age_group_list:
            query = query.filter(
                ExcessMortalitySummary.age_group == age_group_list)
        results = []
        for row, county_name in query.all():
            if sub_metric == "total":
                value = row.total_excess
            elif sub_metric == "fire":
                value = row.fire_excess
            elif sub_metric == "nonfire":
                value = row.nonfire_excess
            else:
                value = row.total_excess
            results.append({
                "fips": row.fips,
                "county_name": county_name,
                "year": row.year,
                "age_group": row.age_group,
                "population": int(row.population) if row.population is not None else 0,
                "excess_mortality": float(value) if value is not None else 0.0,
                "total_excess": float(row.total_excess) if row.total_excess is not None else 0.0,
                "fire_excess": float(row.fire_excess) if row.fire_excess is not None else 0.0,
                "nonfire_excess": float(row.nonfire_excess) if row.nonfire_excess is not None else 0.0
            })
        return results
    except Exception as e:
        logger.error(
            "Error in excess mortality summary endpoint: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/counties/exceedance")
def get_exceedance_summary(db: Session = Depends(get_db)):
    """
    Return exceedance summary for all counties as GeoJSON.
    """
    try:
        results = db.query(
            County.fips,
            County.name,
            County.geometry,
            ExceedanceSummary.threshold_8,
            ExceedanceSummary.threshold_9
        ).join(
            ExceedanceSummary, County.fips == ExceedanceSummary.fips
        ).all()

        features = []
        for row in results:
            features.append({
                "type": "Feature",
                "properties": {
                    "fips": row.fips,
                    "county_name": row.name,
                    "threshold_8": row.threshold_8,
                    "threshold_9": row.threshold_9,
                }
            })
        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "description": "Exceedance summary for regulatory support",
                "thresholds": [8, 9],
                "tier_meanings": {
                    0: "Below the threshold",
                    1: "Exceeding due to fire smoke on Tier 1 days",
                    2: "Exceeding due to fire smoke on Tier 1&2 days",
                    3: "Exceeding due to fire smoke on Tier 1,2,3 days",
                    4: "Exceeding even after excluding fire smoke on all Tier 1,2,3 days"
                }
            }
        }
    except Exception as e:
        logger.error("Error in exceedance summary endpoint: %s",
                     str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# Health check endpoint


@app.get("/api/download/pm25")
def download_pm25_data(
    time_scale: str = Query(...,
                            description="Time scale: daily, yearly, monthly, or seasonal"),
    start_year: int = Query(..., ge=2006, le=2023,
                            description="Start year (2006-2023)"),
    end_year: int = Query(..., ge=2006, le=2023,
                          description="End year (2006-2023)"),
    counties: Optional[str] = Query(None, description="Comma-separated FIPS codes or 'all'"),
    states: Optional[str] = Query(None, description="Comma-separated state names or 'all'"),
    db: Session = Depends(get_db)
):
    """
    Download PM₂.₅ data as CSV for the specified time period and scale.
    Supports filtering by county FIPS codes or state names.
    """
    try:
        if start_year > end_year:
            raise HTTPException(
                status_code=400, detail="Start year must be less than or equal to end year")

        # Parse county and state filters
        county_fips_list = None
        state_fips_prefix_list = None

        if counties and counties.lower() != 'all':
            county_fips_list = [c.strip() for c in counties.split(',')]

        if states and states.lower() != 'all':
            # Convert state names to FIPS prefixes (first 2 digits)
            # We'll need to filter by FIPS prefix
            state_names = [s.strip().lower() for s in states.split(',')]
            # Get all counties and filter by state
            all_counties = db.query(County).all()
            state_fips_prefix_list = []
            for county in all_counties:
                # Extract state name from county name or use state FIPS prefix
                # For now, we'll use FIPS prefix (first 2 digits)
                if len(county.fips) >= 2:
                    state_fips = county.fips[:2]
                    if state_fips not in state_fips_prefix_list:
                        state_fips_prefix_list.append(state_fips)

        # Handle daily data separately
        if time_scale == "daily":
            query = db.query(
                County.fips,
                County.name,
                DailyPM25.date,
                DailyPM25.total,
                DailyPM25.fire,
                DailyPM25.nonfire
            ).join(
                DailyPM25, County.fips == DailyPM25.fips
            ).filter(
                func.extract('year', DailyPM25.date) >= start_year,
                func.extract('year', DailyPM25.date) <= end_year
            )

            # Apply county/state filters
            if county_fips_list:
                query = query.filter(County.fips.in_(county_fips_list))
            elif state_fips_prefix_list:
                query = query.filter(func.substring(County.fips, 1, 2).in_(state_fips_prefix_list))

            query = query.order_by(County.fips, DailyPM25.date)

        else:
            # Determine which summary table to use based on time scale
            if time_scale == "yearly":
                summary_table = YearlyPM25Summary
                time_column = summary_table.year
            elif time_scale == "monthly":
                summary_table = MonthlyPM25Summary
                time_column = summary_table.month
            elif time_scale == "seasonal":
                summary_table = SeasonalPM25Summary
                time_column = summary_table.season
            else:
                raise HTTPException(
                    status_code=400, detail="Invalid time scale. Use 'daily', 'yearly', 'monthly', or 'seasonal'")

            # Query data with county information (excluding pop_weighted)
            query = db.query(
                County.fips,
                County.name,
                summary_table.year,
                time_column,
                summary_table.avg_total,
                summary_table.avg_fire,
                summary_table.avg_nonfire,
                summary_table.max_total,
                summary_table.max_fire,
                summary_table.max_nonfire,
                summary_table.days_count
            ).join(
                summary_table, County.fips == summary_table.fips
            ).filter(
                summary_table.year >= start_year,
                summary_table.year <= end_year
            )

            # Apply county/state filters
            if county_fips_list:
                query = query.filter(County.fips.in_(county_fips_list))
            elif state_fips_prefix_list:
                query = query.filter(func.substring(County.fips, 1, 2).in_(state_fips_prefix_list))

            query = query.order_by(County.fips, summary_table.year, time_column)

        results = query.all()

        if not results:
            raise HTTPException(
                status_code=404, detail="No data found for the specified parameters")

        # Generate CSV content
        csv_lines = []

        # CSV headers (excluding pop_weighted)
        if time_scale == "daily":
            headers = ["County_FIPS", "County_Name", "Date", "Total_PM25", "Fire_PM25", "Nonfire_PM25"]
        elif time_scale == "yearly":
            headers = ["County_FIPS", "County_Name", "Year", "Avg_Total_PM25", "Avg_Fire_PM25", "Avg_Nonfire_PM25",
                       "Max_Total_PM25", "Max_Fire_PM25", "Max_Nonfire_PM25", "Days_Count"]
        elif time_scale == "monthly":
            headers = ["County_FIPS", "County_Name", "Year", "Month", "Avg_Total_PM25", "Avg_Fire_PM25", "Avg_Nonfire_PM25",
                       "Max_Total_PM25", "Max_Fire_PM25", "Max_Nonfire_PM25", "Days_Count"]
        else:  # seasonal
            headers = ["County_FIPS", "County_Name", "Year", "Season", "Avg_Total_PM25", "Avg_Fire_PM25", "Avg_Nonfire_PM25",
                       "Max_Total_PM25", "Max_Fire_PM25", "Max_Nonfire_PM25", "Days_Count"]

        csv_lines.append(",".join(headers))

        # CSV data rows
        for row in results:
            if time_scale == "daily":
                csv_line = [
                    row.fips,
                    f'"{row.name}"',  # Quote county names to handle commas
                    row.date.strftime('%Y-%m-%d'),
                    f"{row.total:.2f}" if row.total is not None else "",
                    f"{row.fire:.2f}" if row.fire is not None else "",
                    f"{row.nonfire:.2f}" if row.nonfire is not None else ""
                ]
            elif time_scale == "yearly":
                csv_line = [
                    row.fips,
                    f'"{row.name}"',  # Quote county names to handle commas
                    str(row.year),
                    f"{row.avg_total:.2f}" if row.avg_total is not None else "",
                    f"{row.avg_fire:.2f}" if row.avg_fire is not None else "",
                    f"{row.avg_nonfire:.2f}" if row.avg_nonfire is not None else "",
                    f"{row.max_total:.2f}" if row.max_total is not None else "",
                    f"{row.max_fire:.2f}" if row.max_fire is not None else "",
                    f"{row.max_nonfire:.2f}" if row.max_nonfire is not None else "",
                    str(row.days_count) if row.days_count is not None else ""
                ]
            elif time_scale == "monthly":
                csv_line = [
                    row.fips,
                    f'"{row.name}"',
                    str(row.year),
                    str(row.month),
                    f"{row.avg_total:.2f}" if row.avg_total is not None else "",
                    f"{row.avg_fire:.2f}" if row.avg_fire is not None else "",
                    f"{row.avg_nonfire:.2f}" if row.avg_nonfire is not None else "",
                    f"{row.max_total:.2f}" if row.max_total is not None else "",
                    f"{row.max_fire:.2f}" if row.max_fire is not None else "",
                    f"{row.max_nonfire:.2f}" if row.max_nonfire is not None else "",
                    str(row.days_count) if row.days_count is not None else ""
                ]
            else:  # seasonal
                csv_line = [
                    row.fips,
                    f'"{row.name}"',
                    str(row.year),
                    str(row.season),
                    f"{row.avg_total:.2f}" if row.avg_total is not None else "",
                    f"{row.avg_fire:.2f}" if row.avg_fire is not None else "",
                    f"{row.avg_nonfire:.2f}" if row.avg_nonfire is not None else "",
                    f"{row.max_total:.2f}" if row.max_total is not None else "",
                    f"{row.max_fire:.2f}" if row.max_fire is not None else "",
                    f"{row.max_nonfire:.2f}" if row.max_nonfire is not None else "",
                    str(row.days_count) if row.days_count is not None else ""
                ]

            csv_lines.append(",".join(csv_line))

        csv_content = "\n".join(csv_lines)

        # Return CSV response
        filename = f"pm25_data_{time_scale}_{start_year}_{end_year}.csv"

        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in PM₂.₅ download endpoint: %s",
                     str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/mortality")
def download_mortality_data(
    start_year: int = Query(..., ge=2006, le=2023,
                            description="Start year (2006-2023)"),
    end_year: int = Query(..., ge=2006, le=2023,
                          description="End year (2006-2023)"),
    age_groups: Optional[str] = Query(
        None, description="Comma-separated age group indices (e.g., '1,2,3')"),
    db: Session = Depends(get_db)
):
    """
    Download excess mortality data as CSV for the specified time period and age groups.
    """
    try:
        if start_year > end_year:
            raise HTTPException(
                status_code=400, detail="Start year must be less than or equal to end year")

        # Parse age groups if provided
        age_group_list = None
        if age_groups:
            try:
                age_group_list = [int(ag.strip())
                                  for ag in age_groups.split(",")]
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid age group format. Use comma-separated integers.")

        # Build query
        query = db.query(
            County.fips,
            County.name,
            ExcessMortalitySummary.year,
            ExcessMortalitySummary.age_group,
            ExcessMortalitySummary.population,
            ExcessMortalitySummary.total_excess,
            ExcessMortalitySummary.fire_excess,
            ExcessMortalitySummary.nonfire_excess
        ).join(
            ExcessMortalitySummary, County.fips == ExcessMortalitySummary.fips
        ).filter(
            ExcessMortalitySummary.year >= start_year,
            ExcessMortalitySummary.year <= end_year
        )

        # Filter by age groups if specified
        if age_group_list:
            query = query.filter(
                ExcessMortalitySummary.age_group.in_(age_group_list))

        # Order results
        query = query.order_by(
            County.fips, ExcessMortalitySummary.year, ExcessMortalitySummary.age_group)

        results = query.all()

        if not results:
            raise HTTPException(
                status_code=404, detail="No data found for the specified parameters")

        # Generate CSV content
        csv_lines = []

        # CSV headers
        headers = ["County_FIPS", "County_Name", "Year", "Age_Group", "Population", "Total_Excess_Mortality",
                   "Fire_Excess_Mortality", "Nonfire_Excess_Mortality"]
        csv_lines.append(",".join(headers))

        # Age group mapping for readable output
        age_group_names = {
            1: "0-4", 2: "5-9", 3: "10-14", 4: "15-19", 5: "20-24", 6: "25-29", 7: "30-34", 8: "35-39",
            9: "40-44", 10: "45-49", 11: "50-54", 12: "55-59", 13: "60-64", 14: "65-69", 15: "70-74",
            16: "75-79", 17: "80-84", 18: "85+"
        }

        # CSV data rows
        for row in results:
            age_group_name = age_group_names.get(
                row.age_group, f"Group_{row.age_group}")

            csv_line = [
                row.fips,
                f'"{row.name}"',  # Quote county names to handle commas
                str(row.year),
                age_group_name,
                str(row.population) if row.population is not None else "",
                f"{row.total_excess:.3f}" if row.total_excess is not None else "",
                f"{row.fire_excess:.3f}" if row.fire_excess is not None else "",
                f"{row.nonfire_excess:.3f}" if row.nonfire_excess is not None else ""
            ]

            csv_lines.append(",".join(csv_line))

        csv_content = "\n".join(csv_lines)

        # Return CSV response
        filename = f"mortality_data_{start_year}_{end_year}.csv"

        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in mortality download endpoint: %s",
                     str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/yll")
def download_yll_data(
    start_year: int = Query(..., ge=2006, le=2023,
                            description="Start year (2006-2023)"),
    end_year: int = Query(..., ge=2006, le=2023,
                          description="End year (2006-2023)"),
    age_groups: Optional[str] = Query(
        None, description="Comma-separated age group indices (e.g., '1,2,3')"),
    db: Session = Depends(get_db)
):
    """
    Download Years of Life Lost (YLL) data as CSV for the specified time period and age groups.
    """
    try:
        if start_year > end_year:
            raise HTTPException(
                status_code=400, detail="Start year must be less than or equal to end year")

        # Parse age groups if provided
        age_group_list = None
        if age_groups:
            try:
                age_group_list = [int(ag.strip())
                                  for ag in age_groups.split(",")]
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid age group format. Use comma-separated integers.")

        # Build query - we'll calculate YLL from excess mortality and life expectancy
        query = db.query(
            County.fips,
            County.name,
            ExcessMortalitySummary.year,
            ExcessMortalitySummary.age_group,
            ExcessMortalitySummary.population,
            ExcessMortalitySummary.total_excess,
            ExcessMortalitySummary.fire_excess,
            ExcessMortalitySummary.nonfire_excess
        ).join(
            ExcessMortalitySummary, County.fips == ExcessMortalitySummary.fips
        ).filter(
            ExcessMortalitySummary.year >= start_year,
            ExcessMortalitySummary.year <= end_year
        )

        # Filter by age groups if specified
        if age_group_list:
            query = query.filter(
                ExcessMortalitySummary.age_group.in_(age_group_list))

        # Order results
        query = query.order_by(
            County.fips, ExcessMortalitySummary.year, ExcessMortalitySummary.age_group)

        results = query.all()

        if not results:
            raise HTTPException(
                status_code=404, detail="No data found for the specified parameters")

        # Generate CSV content
        csv_lines = []

        # CSV headers
        headers = ["County_FIPS", "County_Name", "Year", "Age_Group", "Population",
                   "Total_YLL", "Fire_YLL", "Nonfire_YLL", "Life_Expectancy"]
        csv_lines.append(",".join(headers))

        # Age group mapping and life expectancy estimates
        age_group_names = {
            1: "0-4", 2: "5-9", 3: "10-14", 4: "15-19", 5: "20-24", 6: "25-29", 7: "30-34", 8: "35-39",
            9: "40-44", 10: "45-49", 11: "50-54", 12: "55-59", 13: "60-64", 14: "65-69", 15: "70-74",
            16: "75-79", 17: "80-84", 18: "85+"
        }

        # Approximate life expectancy by age group (US averages)
        life_expectancy_by_age = {
            1: 80.0, 2: 75.0, 3: 70.0, 4: 65.0, 5: 60.0, 6: 55.0, 7: 50.0, 8: 45.0,
            9: 40.0, 10: 35.0, 11: 30.0, 12: 25.0, 13: 20.0, 14: 15.0, 15: 10.0, 16: 7.0,
            17: 5.0, 18: 3.0
        }

        # CSV data rows
        for row in results:
            age_group_name = age_group_names.get(
                row.age_group, f"Group_{row.age_group}")
            life_expectancy = life_expectancy_by_age.get(row.age_group, 10.0)

            # Calculate YLL (Years of Life Lost)
            total_yll = (
                row.total_excess * life_expectancy) if row.total_excess is not None else 0.0
            fire_yll = (row.fire_excess *
                        life_expectancy) if row.fire_excess is not None else 0.0
            nonfire_yll = (
                row.nonfire_excess * life_expectancy) if row.nonfire_excess is not None else 0.0

            csv_line = [
                row.fips,
                f'"{row.name}"',  # Quote county names to handle commas
                str(row.year),
                age_group_name,
                str(row.population) if row.population is not None else "",
                f"{total_yll:.1f}" if total_yll is not None else "",
                f"{fire_yll:.1f}" if fire_yll is not None else "",
                f"{nonfire_yll:.1f}" if nonfire_yll is not None else "",
                f"{life_expectancy:.1f}"
            ]

            csv_lines.append(",".join(csv_line))

        csv_content = "\n".join(csv_lines)

        # Return CSV response
        filename = f"yll_data_{start_year}_{end_year}.csv"

        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in YLL download endpoint: %s",
                     str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/states/boundaries")
async def get_state_boundaries():
    """Get state boundaries as GeoJSON for map outlines."""
    try:
        # Load state shapefile
        shapefile_path = "data/shapefiles/state/cb_2024_us_state_5m.shp"
        gdf = gpd.read_file(shapefile_path)

        # Convert to GeoJSON
        geojson = gdf.to_crs('EPSG:4326').to_json()

        return Response(
            content=geojson,
            media_type="application/json"
        )
    except Exception as e:
        logger.error("Error loading state boundaries: %s",
                     str(e), exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error loading state boundaries")


def log_download_request_to_csv(request: DownloadRequest):
    """Log download request to a CSV file."""
    try:
        # Create logs directory if it doesn't exist
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)

        csv_file = log_dir / "download_requests.csv"
        file_exists = csv_file.exists()

        # Append to CSV file
        with open(csv_file, 'a', newline='', encoding='utf-8') as f:
            fieldnames = [
                'timestamp', 'name', 'institution', 'email', 'usage_description',
                'data_type', 'time_scale', 'start_year', 'end_year',
                'counties', 'states', 'age_groups'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)

            # Write header if file is new
            if not file_exists:
                writer.writeheader()

            # Write the request
            writer.writerow({
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'name': request.name,
                'institution': request.institution,
                'email': request.email,
                'usage_description': request.usage_description,
                'data_type': request.data_type,
                'time_scale': request.time_scale,
                'start_year': request.start_year,
                'end_year': request.end_year,
                'counties': request.counties or 'All',
                'states': request.states or 'All',
                'age_groups': request.age_groups or 'All'
            })

        logger.info(f"Download request logged to CSV: {request.email}")

    except Exception as e:
        logger.error(f"Failed to log download request to CSV: {str(e)}")




@app.post("/api/download/request")
async def submit_download_request(request: DownloadRequest, background_tasks: BackgroundTasks):
    """
    Submit a download request with user information.
    Logs request to CSV and returns download parameters.
    """
    try:
        # Log request to CSV in background
        background_tasks.add_task(log_download_request_to_csv, request)

        # Return success with download parameters
        return {
            "status": "success",
            "message": "Download request submitted successfully",
            "download_params": {
                "data_type": request.data_type,
                "time_scale": request.time_scale,
                "start_year": request.start_year,
                "end_year": request.end_year,
                "counties": request.counties,
                "states": request.states,
                "age_groups": request.age_groups
            }
        }
    except Exception as e:
        logger.error(f"Error processing download request: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download-requests/export")
async def export_download_requests(api_key: str = Query(..., description="Admin API key")):
    """
    Export all download request logs as CSV.
    Protected endpoint - requires admin API key.

    Usage: /api/download-requests/export?api_key=YOUR_ADMIN_API_KEY
    """
    try:
        # Check API key
        admin_api_key = os.getenv("ADMIN_API_KEY")
        if not admin_api_key:
            raise HTTPException(
                status_code=500,
                detail="Admin API key not configured on server"
            )

        if api_key != admin_api_key:
            logger.warning(f"Invalid API key attempt to access download logs")
            raise HTTPException(
                status_code=401,
                detail="Invalid API key"
            )

        csv_file = Path("logs/download_requests.csv")

        if not csv_file.exists():
            raise HTTPException(
                status_code=404,
                detail="No download requests logged yet"
            )

        # Read the CSV file
        with open(csv_file, 'r', encoding='utf-8') as f:
            csv_content = f.read()

        logger.info("Download requests exported successfully")

        # Return as downloadable CSV
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=download_requests_{datetime.now().strftime('%Y%m%d')}.csv"
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting download requests: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


def insert_pageview(path: str, title: Optional[str], ip: str):
    """Records a pageview. Runs in a background task with its own short-lived
    session so it never holds up the request/response cycle. `ip` is used
    only to resolve a country via iptocc (offline, data embedded in the
    wheel, no network call) and is never itself persisted."""
    try:
        country = country_code(ip)
    except Exception:
        country = None  # unresolvable IP (e.g. "unknown", private range, malformed) - store no country rather than fail the insert
    db = SessionLocal()
    try:
        db.add(PageView(path=path, title=title, country=country))
        db.commit()
    except SQLAlchemyError as e:
        logger.error("Failed to record pageview: %s", str(e))
        db.rollback()
    finally:
        db.close()


# Simple in-memory per-IP rate limit for the tracking endpoint. Paths are
# restricted to a fixed allowlist (see VALID_PAGEVIEW_PATHS), so the only
# remaining thing to bound is volume of legitimate-looking requests.
_pageview_hits: dict[str, list[float]] = defaultdict(list)
PAGEVIEW_RATE_LIMIT = 10
PAGEVIEW_RATE_WINDOW_SECS = 60


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _pageview_hits[ip] if now - t < PAGEVIEW_RATE_WINDOW_SECS]
    recent.append(now)
    _pageview_hits[ip] = recent
    return len(recent) > PAGEVIEW_RATE_LIMIT


@app.post("/api/track-view")
async def track_view(view: PageViewRequest, request: Request, background_tasks: BackgroundTasks):
    """Records a pageview for basic visit counting. Path is restricted to a
    fixed allowlist and requests are rate-limited per IP to guard against
    spam/inflation. The insert happens in the background so this always
    returns immediately regardless of DB latency."""
    client_ip = _get_client_ip(request)
    if _is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests")
    background_tasks.add_task(insert_pageview, view.path, VALID_PAGEVIEW_PATHS[view.path], client_ip)
    return {"status": "ok"}


@app.get("/api/view-counts")
async def get_view_counts(
    api_key: str = Query(..., description="Admin API key"),
    db: Session = Depends(get_db)
):
    """
    Returns total pageviews and a breakdown by path.
    Protected endpoint - requires admin API key.

    Usage: /api/view-counts?api_key=YOUR_ADMIN_API_KEY
    """
    admin_api_key = os.getenv("ADMIN_API_KEY")
    if not admin_api_key:
        raise HTTPException(
            status_code=500,
            detail="Admin API key not configured on server"
        )

    if api_key != admin_api_key:
        logger.warning("Invalid API key attempt to access view counts")
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    total = db.query(func.count(PageView.id)).scalar()
    by_path = db.query(PageView.path, func.count(PageView.id)).group_by(PageView.path).all()
    return {"total": total, "by_path": {path: count for path, count in by_path}}


def require_admin_key(request: Request, api_key: Optional[str] = Query(None)):
    """Gates admin-only endpoints behind the ADMIN_API_KEY shared secret.
    Accepts either the X-Admin-Key header (used by the frontend admin page,
    so the key doesn't end up in server access logs) or the `api_key` query
    param (matching the convention used by /api/view-counts and
    /api/download-requests/export, and useful for visiting the endpoint
    directly by URL). Fails closed if ADMIN_API_KEY itself isn't configured."""
    admin_api_key = os.getenv("ADMIN_API_KEY")
    if not admin_api_key:
        raise HTTPException(status_code=500, detail="Admin API key not configured on server")
    supplied = request.headers.get("x-admin-key") or api_key
    if supplied != admin_api_key:
        logger.warning("Invalid API key attempt to access pageview log")
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/api/pageviews", dependencies=[Depends(require_admin_key)])
async def get_pageviews(
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """
    Admin-only: lists individual pageview rows (path, title, country,
    timestamp), newest first. Protected by require_admin_key since this
    exposes granular, timestamped visit data.
    """
    
    rows = (
        db.query(PageView)
        .order_by(PageView.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "path": row.path,
            "title": row.title,
            "country": row.country,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    # Configure Uvicorn logging
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["loggers"]["uvicorn.error"]["level"] = "WARNING"
    log_config["loggers"]["uvicorn.access"]["level"] = "WARNING"
    log_config["loggers"]["uvicorn"]["level"] = "WARNING"

    # Disable watchfiles logging
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="warning",
        log_config=log_config
    )
