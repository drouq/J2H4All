"""Garmin Connect API paths (undocumented; PRD §7).

Single source of truth for endpoint paths so a Garmin-side change is a
one-file fix. Mapped from the reference implementations
(python-garminconnect / Taxuspt garmin_mcp per PRD §7); confirmed against
the live API during first sync.
"""

# Activities — paginated with ?start=N&limit=M (list payload carries most summary fields)
ACTIVITIES_SEARCH = "/activitylist-service/activities/search/activities"
ACTIVITY_DETAIL = "/activity-service/activity/{activity_id}"  # summaryDTO carries feel/RPE
ACTIVITY_SPLITS = "/activity-service/activity/{activity_id}/splits"
ACTIVITY_HR_ZONES = "/activity-service/activity/{activity_id}/hrTimeInZones"
# Per-second sample stream (HR/speed/distance/…); downsampled via maxChartSize.
ACTIVITY_DETAILS = "/activity-service/activity/{activity_id}/details?maxChartSize=1000&maxPolylineSize=1000"

# Per-day wellness (paths as used by garth 0.8 data classes — verified locally)
DAILY_SUMMARY = "/usersummary-service/usersummary/daily/"  # ?calendarDate=YYYY-MM-DD
SLEEP_DAILY = "/wellness-service/wellness/dailySleepData/{username}"  # ?date=YYYY-MM-DD&nonSleepBufferMinutes=60
HRV_DAILY = "/hrv-service/hrv/{date}"

# Fitness markers
MAXMET_DAILY_RANGE = "/metrics-service/metrics/maxmet/daily/{start}/{end}"  # VO2max
RACE_PREDICTIONS_LATEST = "/metrics-service/metrics/racepredictions/latest/{display_name}"
TRAINING_STATUS_AGGREGATED = "/metrics-service/metrics/trainingstatus/aggregated/{date}"  # date is a PATH segment, not a query param
TRAINING_READINESS = "/metrics-service/metrics/trainingreadiness/{date}"  # daily 0-100 readiness (list)
ENDURANCE_SCORE = "/metrics-service/metrics/endurancescore?calendarDate={date}"
HILL_SCORE = "/metrics-service/metrics/hillscore?calendarDate={date}"

# Weight / body composition — range endpoint, chunked
WEIGHT_RANGE = "/weight-service/weight/range/{start}/{end}"  # ?includeAll=true

# Workout push — WRITE endpoints (docs/garmin-workout-push-plan.md; probed live 2026-07-10)
WORKOUT_CREATE = "/workout-service/workout"  # POST payload -> {workoutId}
WORKOUT_ITEM = "/workout-service/workout/{workout_id}"  # GET / PUT (payload must echo workoutId) / DELETE
WORKOUT_SCHEDULE = "/workout-service/schedule/{workout_id}"  # POST {"date": ISO} -> {workoutScheduleId}
WORKOUT_UNSCHEDULE = "/workout-service/schedule/{schedule_id}"  # DELETE
