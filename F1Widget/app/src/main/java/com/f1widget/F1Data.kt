package com.f1widget

import android.util.Log
import org.json.JSONArray
import java.net.URL
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private const val TAG = "F1Widget"

data class F1Session(
    val sessionName: String,
    val sessionType: String,
    val meetingName: String,
    val countryName: String,
    val dateStart: OffsetDateTime
)

data class WidgetDisplay(
    val raceName: String,
    val sessionLabel: String,
    val dayDate: String,
    val time: String
)

object F1DataFetcher {

    private val EASTERN = ZoneId.of("America/New_York")
    private val API_BASE = "https://api.openf1.org/v1/sessions"

    fun fetchNextSession(): F1Session? {
        return try {
            val now = OffsetDateTime.now()
            val year = now.year
            val sessions = mutableListOf<F1Session>()
            sessions.addAll(fetchSessionsForYear(year))
            // Fetch next year too if within last 2 months of season
            if (now.monthValue >= 11) {
                sessions.addAll(fetchSessionsForYear(year + 1))
            }
            sessions.filter { it.dateStart.isAfter(now) }
                .minByOrNull { it.dateStart }
        } catch (e: Exception) {
            Log.e(TAG, "Error fetching next session", e)
            null
        }
    }

    private val RACE_WEEKEND_TYPES = setOf(
        "practice", "qualifying", "sprint", "sprint qualifying",
        "sprint shootout", "race"
    )

    private fun fetchSessionsForYear(year: Int): List<F1Session> {
        val url = "$API_BASE?year=$year"
        val json = URL(url).readText(Charsets.UTF_8)
        val arr = JSONArray(json)
        val sessions = mutableListOf<F1Session>()
        for (i in 0 until arr.length()) {
            try {
                val obj = arr.getJSONObject(i)
                val dateStartStr = obj.getString("date_start")
                val sessionType = obj.optString("session_type", "")
                if (sessionType.lowercase() !in RACE_WEEKEND_TYPES) continue
                val dateStart = OffsetDateTime.parse(dateStartStr)
                sessions.add(
                    F1Session(
                        sessionName = obj.optString("session_name", "Unknown"),
                        sessionType = sessionType,
                        meetingName = obj.optString("meeting_name", "Unknown GP"),
                        countryName = obj.optString("country_name", ""),
                        dateStart = dateStart
                    )
                )
            } catch (e: Exception) {
                Log.w(TAG, "Skipping malformed session entry at $i")
            }
        }
        return sessions
    }

    fun formatForWidget(session: F1Session): WidgetDisplay {
        val eastern = session.dateStart.atZoneSameInstant(EASTERN)
        val dayDate = eastern.format(DateTimeFormatter.ofPattern("EEE, MMM d")).uppercase()
        val time = eastern.format(DateTimeFormatter.ofPattern("h:mm a 'ET'"))

        val sessionLabel = when {
            session.sessionName.contains("Practice 1", ignoreCase = true) -> "FREE PRACTICE 1"
            session.sessionName.contains("Practice 2", ignoreCase = true) -> "FREE PRACTICE 2"
            session.sessionName.contains("Practice 3", ignoreCase = true) -> "FREE PRACTICE 3"
            session.sessionName.contains("Sprint Qualifying", ignoreCase = true) -> "SPRINT QUALIFYING"
            session.sessionName.contains("Sprint Shootout", ignoreCase = true) -> "SPRINT SHOOTOUT"
            session.sessionName.contains("Sprint", ignoreCase = true) -> "SPRINT"
            session.sessionName.contains("Qualifying", ignoreCase = true) -> "QUALIFYING"
            session.sessionName.contains("Race", ignoreCase = true) -> "RACE"
            else -> session.sessionName.uppercase()
        }

        // Shorten "Grand Prix" to "GP" for display
        val shortName = session.meetingName
            .replace("Grand Prix", "GP")
            .replace("Formula 1", "F1")
            .trim()

        return WidgetDisplay(shortName, sessionLabel, dayDate, time)
    }
}
