package com.f1widget

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters

class WidgetUpdateWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val session = F1DataFetcher.fetchNextSession() ?: return Result.retry()

        val display = F1DataFetcher.formatForWidget(session)

        applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_RACE_NAME, display.raceName)
            .putString(KEY_SESSION_LABEL, display.sessionLabel)
            .putString(KEY_DAY_DATE, display.dayDate)
            .putString(KEY_TIME, display.time)
            .apply()

        val manager = AppWidgetManager.getInstance(applicationContext)
        val ids = manager.getAppWidgetIds(
            ComponentName(applicationContext, F1WidgetProvider::class.java)
        )
        for (id in ids) {
            F1WidgetProvider.updateAppWidget(applicationContext, manager, id)
        }

        return Result.success()
    }

    companion object {
        const val PREFS_NAME = "f1widget_prefs"
        const val KEY_RACE_NAME = "race_name"
        const val KEY_SESSION_LABEL = "session_label"
        const val KEY_DAY_DATE = "day_date"
        const val KEY_TIME = "time"
    }
}
