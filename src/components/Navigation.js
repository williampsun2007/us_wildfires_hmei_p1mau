import React from 'react';
import { Box, Tabs, Tab, Typography, Link } from '@mui/material';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';

function Navigation({ activeTab, onTabChange }) {
    const handleChange = (event, newValue) => {
        onTabChange(newValue);
    };

    return (
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' }}>
            <img
                src="/PU_lockup.png"
                alt="App Logo"
                className="app-logo"
            />
            <Typography variant="h4" component="h1" sx={{ fontWeight: 'bold', flex: 1, textAlign: 'center' }}>
                PM2.5 Wildfire Smoke Impact Map
            </Typography>
            <Tabs
                value={activeTab}
                onChange={handleChange}
                sx={{
                    '& .MuiTab-root': {
                        color: 'rgba(0, 0, 0, 0.7)',
                        fontWeight: 500,
                        fontSize: '0.9rem',
                        textTransform: 'none',
                        minWidth: 'auto',
                        padding: '6px 16px',
                        fontFamily: 'Roboto, sans-serif',
                        '&.Mui-selected': {
                            color: '#333',
                            fontWeight: 600,
                            backgroundColor: 'transparent',
                        },
                        '&:hover': {
                            color: '#333',
                            backgroundColor: 'rgba(255, 255, 255, 0.6)',
                            borderRadius: '4px',
                        }
                    },
                    '& .MuiTabs-indicator': {
                        backgroundColor: '#333',
                        height: '3px',
                    }
                }}
            >
                <Tab label="Map" value="map" />
                <Tab label="About" value="about" />
                <Tab label="Partners" value="partners" />
                <Tab label="Data & Methodology" value="methodology" />
            </Tabs>
            <Link
                href="https://williampsun-pm25-exposure-tool.hf.space/"
                target="_blank"
                rel="noopener noreferrer"
                sx={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 0.4,
                    ml: 2,
                    flexShrink: 0,
                    color: 'rgba(0, 0, 0, 0.7)',
                    fontWeight: 500,
                    fontSize: '0.85rem',
                    fontFamily: 'Roboto, sans-serif',
                    textDecoration: 'none',
                    whiteSpace: 'nowrap',
                    padding: '6px 10px',
                    borderRadius: '4px',
                    '&:hover': {
                        color: '#333',
                        backgroundColor: 'rgba(255, 255, 255, 0.6)',
                    },
                }}
            >
                Related Tool
                <OpenInNewIcon sx={{ fontSize: '0.9rem' }} />
            </Link>
        </Box>
    );
}

export default Navigation; 