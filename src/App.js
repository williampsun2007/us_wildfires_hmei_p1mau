import React, { useState, useEffect } from 'react';
import { Box, Container, CssBaseline, Typography, Button, Dialog, DialogTitle, DialogContent, DialogActions } from '@mui/material';
import Map from './components/Map';
import Sidebar from './components/Sidebar';
import CountyBarChart from './components/CountyBarChart';
import Navigation from './components/Navigation';
import About from './components/About';
import Partners from './components/Partners';
import Methodology from './components/Methodology';
import { apiCall } from './utils/api';
import { trackPageview } from './utils/analytics';
import './styles/style.css';

const PM25_LAYERS = ['average', 'max', 'pop_weighted'];
const HEALTH_LAYERS = ['mortality', 'yll', 'population'];

const AGE_GROUPS = [
  { value: 1, label: '0-4' },
  { value: 2, label: '5-9' },
  { value: 3, label: '10-14' },
  { value: 4, label: '15-19' },
  { value: 5, label: '20-24' },
  { value: 6, label: '25-29' },
  { value: 7, label: '30-34' },
  { value: 8, label: '35-39' },
  { value: 9, label: '40-44' },
  { value: 10, label: '45-49' },
  { value: 11, label: '50-54' },
  { value: 12, label: '55-59' },
  { value: 13, label: '60-64' },
  { value: 14, label: '65-69' },
  { value: 15, label: '70-74' },
  { value: 16, label: '75-79' },
  { value: 17, label: '80-84' },
  { value: 18, label: '85+' },
];

function App() {
  const [error, setError] = useState(null);
  // Single active layer and sublayer
  const [activeLayer, setActiveLayer] = useState('average'); // e.g., 'average', 'max', 'pop_weighted', 'mortality', 'population'
  const [pm25SubLayer, setPm25SubLayer] = useState('total'); // 'total', 'fire', 'nonfire'
  const [timeControls, setTimeControls] = useState({ timeScale: 'yearly', year: 2023, month: 1, season: 'winter' });
  const [selectedCounty, setSelectedCounty] = useState(null); // Clicked county
  const [hoveredCounty, setHoveredCounty] = useState(null); // Hovered county
  const [lastHoveredCounty, setLastHoveredCounty] = useState(null); // Store last full hover data
  const [loading, setLoading] = useState(false);
  const [mapRefreshKey, setMapRefreshKey] = useState(0); // triggers map data refresh
  const [sidebarWidth, setSidebarWidth] = useState(400); // Track sidebar width for map adjustment

  const [selectedAgeGroups, setSelectedAgeGroups] = useState(AGE_GROUPS.map(g => g.value)); // default to all
  const [openLanding, setOpenLanding] = useState(true); // modal opens on first load
  const [mortalitySubMetric, setMortalitySubMetric] = useState('total'); // 'total', 'fire', 'nonfire'
  const [activeTab, setActiveTab] = useState('map'); // 'map', 'about', 'partners', 'methodology'



  useEffect(() => {
    if (activeLayer === 'mortality') {
      setMortalitySubMetric('total');
    }
  }, [activeLayer]);

  useEffect(() => {
    const titles = { map: 'Map', about: 'About', partners: 'Partners', methodology: 'Data & Methodology' };
    trackPageview(`/${activeTab}`, titles[activeTab]);
  }, [activeTab]);

  // Handler for layer selection
  const handleSetActiveLayer = (layer) => {
    if (PM25_LAYERS.includes(layer)) {
      setActiveLayer(layer);
      setPm25SubLayer('total');
    } else if (HEALTH_LAYERS.includes(layer)) {
      setActiveLayer(layer);
      setPm25SubLayer('');
    } else {
      setActiveLayer(layer);
    }
  };
  // Handler for PM2.5 sublayer
  const handleSetPm25SubLayer = (subLayer) => {
    setPm25SubLayer(subLayer);
    // If not on a PM2.5 layer, switch to default PM2.5
    if (!PM25_LAYERS.includes(activeLayer)) {
      setActiveLayer('average');
    }
  };
  const handleTimeControlsChange = (controls) => {
    setTimeControls(controls);
    setSelectedCounty(null); // Clear county info on time scale change
  };
  const handleMapLoaded = () => {
    setLoading(false);
  };
  // Handler for hover
  const handleCountyHover = (county) => {
    setHoveredCounty(county);
    if (county) setLastHoveredCounty(county);
  };
  // Handler for click: use lastHoveredCounty
  const handleCountySelect = (county) => {
    if (selectedCounty && county && selectedCounty.fips === selectedCounty.fips) {
      setSelectedCounty(null); // Deselect if clicking same county
    } else if (lastHoveredCounty && county && lastHoveredCounty.fips === county.fips) {
      setSelectedCounty(lastHoveredCounty);
    } else if (county) {
      setSelectedCounty(county); // fallback, should always have lastHoveredCounty
    }
  };
  const handleSetMortalitySubMetric = (subMetric) => {
    setMortalitySubMetric(subMetric);
  };

  // Handler for sidebar width changes
  const handleSidebarWidthChange = (newWidth) => {
    setSidebarWidth(newWidth);
  };

  // Fetch bar chart data for selected county
  const fetchBarChartData = async (county, timeControls) => {
    if (!county || !county.fips) return null;
    let params = new URLSearchParams();
    let endpoint = `/api/pm25/bar_chart/${county.fips}`;
    const { timeScale, year, month, season } = timeControls;
    if (timeScale === 'yearly') {
      params.append('time_scale', 'yearly');
      params.append('start_year', '2013');
      params.append('end_year', '2023');
    } else if (timeScale === 'monthly') {
      params.append('time_scale', 'daily');
      params.append('year', year.toString());
      params.append('month', month.toString());
      const startDate = new Date(year, month - 1, 1);
      const endDate = new Date(year, month, 1);
      params.append('start_date', startDate.toISOString().split('T')[0]);
      params.append('end_date', endDate.toISOString().split('T')[0]);
    } else if (timeScale === 'seasonal') {
      params.append('time_scale', 'daily');
      params.append('year', year.toString());
      params.append('season', season);
      let startDate, endDate;
      if (season === 'winter') {
        startDate = new Date(year - 1, 11, 21);
        endDate = new Date(year, 2, 21);
      } else if (season === 'spring') {
        startDate = new Date(year, 2, 21);
        endDate = new Date(year, 5, 21);
      } else if (season === 'summer') {
        startDate = new Date(year, 5, 21);
        endDate = new Date(year, 8, 21);
      } else if (season === 'fall') {
        startDate = new Date(year, 8, 21);
        endDate = new Date(year, 11, 21);
      }
      params.append('start_date', startDate.toISOString().split('T')[0]);
      params.append('end_date', endDate.toISOString().split('T')[0]);
    }
    const response = await fetch(`${process.env.REACT_APP_API_BASE_URL || ''}${endpoint}?${params}`);
    if (!response.ok) return null;
    const data = await response.json();
    return data;
  };

  // Handler to clear selection (for X button)
  const handleClearSelectedCounty = () => {
    setSelectedCounty(null);
  };

  // Handler for tab changes
  const handleTabChange = (newTab) => {
    setActiveTab(newTab);
  };



  if (error) {
    return (
      <Container maxWidth="md" sx={{ mt: 4, textAlign: 'center' }}>
        <Typography color="error" variant="h5" gutterBottom>
          Error
        </Typography>
        <Typography color="textSecondary" paragraph>
          {error}
        </Typography>
      </Container>
    );
  }

  return (
    <>
      <CssBaseline />
      {/* Landing Modal */}
      <Dialog open={openLanding} onClose={() => setOpenLanding(false)} maxWidth="sm" fullWidth>
        <DialogTitle component="div">
          <Typography
            variant="h5"
            align="center"
            fontWeight="bold"
            fontFamily="Inter, Roboto, Helvetica Neue, sans-serif"
            letterSpacing={0.5}
          >
            PM2.5 Wildfire Smoke Impact Map
          </Typography>
        </DialogTitle>
        <DialogContent>
          <Typography textAlign="center" variant="body1" >
            Welcome! This tool allows you to explore the impact of PM2.5 pollution from wildfire smoke across the United States using interactive maps and charts.
          </Typography>
        </DialogContent>
        <DialogContent>
          <Typography textAlign="center" variant="body2" color="text.secondary">
            <b>How to use:</b> Use the controls on the right to select data layers, time periods, and age groups. Click on a county for detailed information and time series charts.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpenLanding(false)} variant="contained" color="primary">
            Get Started
          </Button>
        </DialogActions>
      </Dialog>
      {/* Header */}
      <Box component="header" className="app-header">
        <Container maxWidth="xl">
          <Navigation activeTab={activeTab} onTabChange={handleTabChange} />
        </Container>
      </Box>

      {/* Main content based on active tab */}
      {activeTab === 'map' && (
        <Box sx={{ display: 'flex', flexDirection: 'row', height: 'calc(100vh - 64px)', width: '100vw', margin: 0, padding: 0, overflow: 'hidden' }}>
          {/* Map Area */}
          <Box sx={{ flex: 1, position: 'relative' }}>
            <Map
              stateAbbr="CA"
              activeLayer={activeLayer}
              pm25SubLayer={pm25SubLayer}
              timeControls={{ ...timeControls, subMetric: mortalitySubMetric }}
              onCountySelect={handleCountySelect}
              onCountyHover={handleCountyHover}
              mapRefreshKey={mapRefreshKey}
              onMapLoaded={handleMapLoaded}
              selectedCounty={selectedCounty}
              selectedAgeGroups={selectedAgeGroups}
              sidebarWidth={sidebarWidth}
            />
          </Box>
          {/* Sidebar */}
          <Sidebar
            activeLayer={activeLayer}
            setActiveLayer={handleSetActiveLayer}
            pm25SubLayer={pm25SubLayer}
            setPm25SubLayer={handleSetPm25SubLayer}
            timeControls={timeControls}
            setTimeControls={handleTimeControlsChange}
            selectedCounty={selectedCounty ? selectedCounty : hoveredCounty}
            hoveredCounty={hoveredCounty}
            loading={loading}
            onClearSelectedCounty={handleClearSelectedCounty}
            selectedAgeGroups={selectedAgeGroups}
            setSelectedAgeGroups={setSelectedAgeGroups}
            mortalitySubMetric={mortalitySubMetric}
            setMortalitySubMetric={setMortalitySubMetric}
            onWidthChange={handleSidebarWidthChange}
          />
        </Box>
      )}

      {activeTab === 'about' && <About onTabChange={handleTabChange} />}
      {activeTab === 'partners' && <Partners onTabChange={handleTabChange} />}
      {activeTab === 'methodology' && <Methodology />}
    </>
  );
}

export default App;
