# NCE Cognitive Dashboard NetBox Plugin

An integration plugin that hooks into NetBox detail pages for **devices**, **racks**, and **sites** to inject a "Cognitive State Panel" populated with real-time operations telemetry from the NCE (Network Cognitive Engine) core.

## Features
- **Operator Stress Tracking**: Dynamic charts showing historical operator VAD fatigue and frustration spikes.
- **Incident Logs**: Real-time streaming of events and operational incidents.
- **Fault Maps**: Interactive nodes detailing predictive failure probabilities.
- **Timeline Playback**: Timeline scrubbers to explore the state of the network at any past timestamp.

## Installation
1. Install this package in your NetBox virtual environment:
   ```bash
   pip install nce-netbox-plugin/
   ```
2. Register the plugin in your NetBox `configuration.py`:
   ```python
   PLUGINS = [
       'nce_netbox_plugin',
   ]
   ```
3. Run migrations and restart NetBox:
   ```bash
   python manage.py migrate
   ```
