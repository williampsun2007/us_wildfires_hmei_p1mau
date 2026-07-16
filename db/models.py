from datetime import date
from sqlalchemy import Column, Integer, Float, String, Boolean, Date, DateTime, ForeignKey, UniqueConstraint, extract, func, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, column_property
from sqlalchemy.ext.hybrid import hybrid_property
from .database import Base

class County(Base):
    __tablename__ = "counties"

    fips = Column(String, primary_key=True, index=True)
    name = Column(String)
    index = Column(Integer, unique=True)
    geometry = Column(JSONB)  # Store GeoJSON as JSONB

    pm25_data = relationship("DailyPM25", back_populates="county")
    populations = relationship("Population", back_populates="county")
    yearly_summaries = relationship("YearlyPM25Summary", back_populates="county")
    monthly_summaries = relationship("MonthlyPM25Summary", back_populates="county")
    seasonal_summaries = relationship("SeasonalPM25Summary", back_populates="county")
    baseline_mortality_rates = relationship("BaselineMortalityRate", back_populates="county")
    cdc_baseline_mortality_rates = relationship("CDCBaselineMortalityRate", back_populates="county")
    exceedance_summaries = relationship("ExceedanceSummary", back_populates="county")
    decomposition_summaries = relationship("DecompositionSummary", back_populates="county")

class DailyPM25(Base):
    __tablename__ = "daily_pm25"
    
    id = Column(Integer, primary_key=True, index=True)
    fips = Column(String, ForeignKey("counties.fips"), index=True, nullable=False)
    county_index = Column(Integer, index=True, nullable=False)  # For easier county matching
    date = Column(Date, index=True, nullable=False)
    total = Column(Float, nullable=False)  # Total PM2.5
    fire = Column(Float, nullable=False)    # Fire-related PM2.5
    nonfire = Column(Float, nullable=False)  # Non-fire PM2.5
    aqi = Column(Integer, nullable=True)  # Calculated AQI
    
    __table_args__ = (UniqueConstraint("fips", "date", name="_fips_date_uc"),)

    county = relationship("County", back_populates="pm25_data")

class AQSData(Base):
    __tablename__ = "aqs_data"

    id = Column(Integer, primary_key=True, index=True)
    fips = Column(String, ForeignKey("counties.fips"), index=True, nullable=False)
    county_index = Column(Integer, index=True, nullable=True)
    date = Column(Date, index=True, nullable=False)
    average_pm = Column(Float, nullable=False)
    aqi = Column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("fips", "date", name="_fips_date_uc_aqs"),)

    county = relationship("County")

    # Hybrid properties for backward compatibility
    @hybrid_property
    def year(self):
        return self.date.year if self.date else None

    @year.expression
    def year(cls):
        return extract('year', cls.date)
        
    # Alias properties for backward compatibility
    @property
    def pm25_value(self):
        return self.total
        
    @property
    def fire_pm25(self):
        return self.fire
        
    @property
    def non_fire_pm25(self):
        return self.nonfire

    @hybrid_property
    def month(self):
        return self.date.month if self.date else None

    @month.expression
    def month(cls):
        return extract('month', cls.date)

    @hybrid_property
    def day(self):
        return self.date.day if self.date else None

    @day.expression
    def day(cls):
        return extract('day', cls.date)

class Population(Base):
    __tablename__ = "population"

    id = Column(Integer, primary_key=True)
    fips = Column(String, ForeignKey("counties.fips"))
    year = Column(Integer, index=True)
    age_group = Column(Integer)
    population = Column(Integer)

    county = relationship("County", back_populates="populations")

    __table_args__ = (
        UniqueConstraint("fips", "year", "age_group", name="_fips_year_age_uc"),
        # Compound index for the main query pattern
        Index('idx_population_fips_year_age', 'fips', 'year', 'age_group'),
        # Filtered index for non-zero age groups
        Index('idx_population_nonzero_age', 'age_group', 
              postgresql_where="age_group != 0"),
    )

class YearlyPM25Summary(Base):
    __tablename__ = "yearly_pm25_summary"
    
    fips = Column(String, ForeignKey("counties.fips"), primary_key=True)
    year = Column(Integer, primary_key=True)
    
    # Aggregated values
    avg_total = Column(Float, nullable=False)
    avg_fire = Column(Float, nullable=False) 
    avg_nonfire = Column(Float, nullable=False)
    
    max_total = Column(Float, nullable=False)
    max_fire = Column(Float, nullable=False)
    max_nonfire = Column(Float, nullable=False)
    
    # Metadata
    days_count = Column(Integer, nullable=False)  # for data quality checks
    
    pop_weighted_total = Column(Float, nullable=True)
    pop_weighted_fire = Column(Float, nullable=True)
    pop_weighted_nonfire = Column(Float, nullable=True)
    
    county = relationship("County", back_populates="yearly_summaries")

    __table_args__ = (
        # Index for the lookup pattern in excess mortality calculation
        Index('idx_yearly_pm25_fips_year', 'fips', 'year'),
    )

class MonthlyPM25Summary(Base):
    __tablename__ = "monthly_pm25_summary"
    
    fips = Column(String, ForeignKey("counties.fips"), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(Integer, primary_key=True)
    
    avg_total = Column(Float, nullable=False)
    avg_fire = Column(Float, nullable=False)
    avg_nonfire = Column(Float, nullable=False)
    
    max_total = Column(Float, nullable=False)
    max_fire = Column(Float, nullable=False)
    max_nonfire = Column(Float, nullable=False)

    days_count = Column(Integer, nullable=False)
    
    pop_weighted_total = Column(Float, nullable=True)
    pop_weighted_fire = Column(Float, nullable=True)
    pop_weighted_nonfire = Column(Float, nullable=True)
    
    county = relationship("County", back_populates="monthly_summaries")

class SeasonalPM25Summary(Base):
    __tablename__ = "seasonal_pm25_summary"
    
    fips = Column(String, ForeignKey("counties.fips"), primary_key=True)
    year = Column(Integer, primary_key=True)
    season = Column(String, primary_key=True)  # 'spring', 'summer', 'fall', 'winter'
    
    avg_total = Column(Float, nullable=False)
    avg_fire = Column(Float, nullable=False)
    avg_nonfire = Column(Float, nullable=False)
    
    max_total = Column(Float, nullable=False)
    max_fire = Column(Float, nullable=False)
    max_nonfire = Column(Float, nullable=False)

    days_count = Column(Integer, nullable=False)
    
    pop_weighted_total = Column(Float, nullable=True)
    pop_weighted_fire = Column(Float, nullable=True)
    pop_weighted_nonfire = Column(Float, nullable=True)
    
    county = relationship("County", back_populates="seasonal_summaries")

class ExcessMortalitySummary(Base):
    __tablename__ = "excess_mortality_summary"

    id = Column(Integer, primary_key=True)
    fips = Column(String, ForeignKey("counties.fips"))

    year = Column(Integer)
    age_group = Column(Integer)
    
    # Keep existing columns as "default" method (GEMM)
    total_excess = Column(Float)
    fire_excess = Column(Float)
    nonfire_excess = Column(Float)
    population = Column(Integer)

    yll_total = Column(Float)
    yll_fire = Column(Float)
    yll_nonfire = Column(Float)
    
    # method-specific columns
    total_gemm = Column(Float)
    fire_gemm = Column(Float)
    nonfire_gemm = Column(Float)
    
    total_boot = Column(Float)
    fire_boot = Column(Float)
    
    total_prec = Column(Float)
    fire_prec = Column(Float)
    
    # YLL for each method
    yll_total_gemm = Column(Float)
    yll_fire_gemm = Column(Float)
    yll_nonfire_gemm = Column(Float)
    
    yll_total_boot = Column(Float)
    yll_fire_boot = Column(Float)
    
    yll_total_prec = Column(Float)
    yll_fire_prec = Column(Float)

    __table_args__ = (
        UniqueConstraint("fips", "year", "age_group", name="_fips_year_agegroup_uc"),
        # Index for bulk insert performance and future queries
        Index('idx_excess_mortality_fips_year', 'fips', 'year'),
        Index('idx_excess_mortality_year_age', 'year', 'age_group'),
    )

class BaselineMortalityRate(Base):
    __tablename__ = "baseline_mortality_rate"

    id = Column(Integer, primary_key=True)
    fips = Column(String, ForeignKey("counties.fips"), index=True)
    county_index = Column(Integer, index=True)

    year = Column(Integer, index=True)
    age_group = Column(Integer)
    stat_type = Column(String)  # '1: mean', '2: upper', '3: lower'
    value = Column(Float)
    source = Column(String)
    allage_flag = Column(Boolean)

    county = relationship("County", back_populates="baseline_mortality_rates")

    __table_args__ = (
        UniqueConstraint("fips", "year", "age_group", "stat_type", "source", name="_unique_mortality_entry"),
        # Compound index for the filtered query we use
        Index('idx_baseline_mortality_filtered_lookup', 'fips', 'year', 'age_group', 
              postgresql_where="source = 'basemor_ALL' AND stat_type = '1' AND allage_flag = false"),
        # Index for the preloading filter
        Index('idx_baseline_mortality_preload_filter', 'source', 'stat_type', 'allage_flag',
              postgresql_where="source = 'basemor_ALL' AND stat_type = '1' AND allage_flag = false"),
    )

class CDCBaselineMortalityRate(Base):
    __tablename__ = "cdc_baseline_mortality_rate"

    id = Column(Integer, primary_key=True)
    fips = Column(String, ForeignKey("counties.fips"), index=True)
    county_index = Column(Integer, index=True)

    year = Column(Integer, index=True)
    age_group = Column(Integer)
    value = Column(Float)
    source = Column(String)

    county = relationship("County", back_populates="cdc_baseline_mortality_rates")

    __table_args__ = (
        UniqueConstraint("fips", "year", "age_group", "source", name="cdc_unique_mortality_entry"),
    )

class ExceedanceSummary(Base):
    """Stores exceedance summary for a county."""
    __tablename__ = "exceedance_summary"

    id = Column(Integer, primary_key=True)
    fips = Column(String, ForeignKey("counties.fips"), index=True, nullable=False)
    county_index = Column(Integer, index=True, nullable=False)

    threshold_9 = Column(Integer, nullable=True)  # Exceeding 9 ug/m3
    threshold_8 = Column(Integer, nullable=True)  # Exceeding 8 ug/m3

    county = relationship("County", back_populates="exceedance_summaries")

    __table_args__ = (UniqueConstraint("fips", name="uq_exceedance_fips"),)

    def __repr__(self):
        return (
            f"<ExceedanceSummary(fips={self.fips}, county_index={self.county_index}, "
            f"threshold_9={self.threshold_9}, threshold_8={self.threshold_8})>"
        )

class DecompositionSummary(Base):
    __tablename__ = "decomposition_summary"

    id = Column(Integer, primary_key=True)
    fips = Column(String, ForeignKey("counties.fips"), index=True)
    start_year = Column(Integer, index=True)
    end_year = Column(Integer, index=True)
    age_group = Column(Integer, nullable=True)  # Optional: null means all ages

    population_growth = Column(Float, nullable=False)
    population_ageing = Column(Float, nullable=False)
    baseline_mortality_change = Column(Float, nullable=False)
    exposure_change = Column(Float, nullable=False)
    total_change = Column(Float, nullable=False)

    county = relationship("County", back_populates="decomposition_summaries")

    __table_args__ = (
        UniqueConstraint("fips", "start_year", "end_year", "age_group", name="_unique_decomp_entry"),
    )

class PageView(Base):
    __tablename__ = "page_views"

    id = Column(Integer, primary_key=True)
    path = Column(String, nullable=False, index=True)
    title = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)

class FireAttributionBin(Base):
    __tablename__ = "fire_attribution_bin"

    id = Column(Integer, primary_key=True)
    method = Column(String, index=True, nullable=False)  # 'bootstrapped_bin_hr' or 'precomputed_bin_af'
    bin_lower = Column(Float, nullable=False)
    bin_upper = Column(Float, nullable=False)
    age_group = Column(String, nullable=False)
    cause = Column(String, nullable=True)  # Only for AF method
    coef = Column(Float, nullable=True)    # Only for HR method
    af = Column(Float, nullable=True)      # Only for AF method
    ci_low = Column(Float, nullable=True)  # Only for AF method
    ci_up = Column(Float, nullable=True)   # Only for AF method
    bootid = Column(Integer, nullable=True)  # Only for HR method (bootstrap replicate)

    __table_args__ = (
        # Keep current index
        Index('ix_fire_attribution_bin_method_age_bin_bootid', 'method', 'age_group', 'bin_lower', 'bin_upper', 'bootid'),
        # Separate indexes for each method type
        Index('idx_fire_bootstrap_lookup', 'age_group', 'bin_lower', 'bin_upper', 'bootid',
              postgresql_where="method = 'bootstrapped_bin_hr'"),
        Index('idx_fire_precomputed_lookup', 'age_group', 'bin_lower', 'bin_upper',
              postgresql_where="method = 'precomputed_bin_af' AND cause = 'Nonaccidental'"),
    )