# Arriva Bus 1.1.5

- Show “Loading data…” / “Gegevens laden…” when tracking starts and while waiting for the first trusted vehicle status.
- Only show “Waiting to depart” once a vehicle event confirms the waiting state. Reported pre-departure delay remains visible on the right.
- Use a gray loading icon without a progress bar. Missing realtime data eventually becomes unavailable instead of loading indefinitely.
- Expose the loading state in the Journey status sensor, including Dutch and English labels.
