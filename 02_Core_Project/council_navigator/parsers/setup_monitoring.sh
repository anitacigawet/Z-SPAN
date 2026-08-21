#!/bin/bash
# Setup automated parser health monitoring

echo "Setting up parser health monitoring..."

# Create monitoring directory
mkdir -p /var/log/parser-health

# Add cron job to run health check daily at 6 AM
(crontab -l 2>/dev/null; echo "0 6 * * * cd /home/ubuntu/arizona-city-council-navigator/parsers && python3 health_monitor.py >> /var/log/parser-health/monitor.log 2>&1") | crontab -

echo "✅ Monitoring setup complete!"
echo "Health checks will run daily at 6:00 AM"
echo "Logs: /var/log/parser-health/monitor.log"
