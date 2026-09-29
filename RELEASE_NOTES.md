# Arriva Bus 1.1.6

- Fix loading remaining visible after timetable data has finished loading.
- Show “Waiting for next bus” with the scheduled stop time until a trusted vehicle status arrives.
- Reserve “Waiting to depart” for a vehicle with a received waiting status; reported delay remains on the right.
- An empty departure list shows “No bus underway”. Waiting for a future journey no longer triggers the initial-data timeout.
- Add the translated waiting_next Journey status for dashboards and automations.
