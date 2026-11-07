# ADR-0005: Develop on Linux (WSL2 Ubuntu 24.04)

Status: accepted · Date: 2026-11-07

## Context
The geospatial toolchain, Docker and the CI runners are Linux-native.

## Decision
- All development, databases and tests run in WSL2 Ubuntu 24.04, on the Linux filesystem.
- Windows desktop GIS applications (ArcGIS Pro, QGIS) connect to the databases through the
  `127.0.0.1` ports.

## Consequences
- Local and CI environments are the same operating system.
- System packages in Ubuntu need `sudo`, which is run by the developer.
