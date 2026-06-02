package com.f1widget

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.widget.RemoteViews
import androidx.work.*
import java.util.concurrent.TimeUnit

class F1WidgetProvider : AppWidgetProvider() {

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        for (id in appWidgetIds) {
            updateAppWidget(context, appWidgetManager, id)
        }
        schedulePeriodicUpdates(context)
        enqueueImmediateUpdate(context)
    }

    override fun onEnabled(context: Context) {
        schedulePeriodicUpdates(context)
        enqueueImmediateUpdate(context)
    }

    override fun onDisabled(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(WORK_NAME_PERIODIC)
    }

    companion object {
        private const val WORK_NAME_PERIODIC = "f1_widget_periodic"
        private const val WORK_NAME_IMMEDIATE = "f1_widget_immediate"

        fun updateAppWidget(
            context: Context,
            appWidgetManager: AppWidgetManager,
            appWidgetId: Int
        ) {
            val prefs = context.getSharedPreferences(
                WidgetUpdateWorker.PREFS_NAME, Context.MODE_PRIVATE
            )
            val raceName = prefs.getString(WidgetUpdateWorker.KEY_RACE_NAME, null)
            val sessionLabel = prefs.getString(WidgetUpdateWorker.KEY_SESSION_LABEL, null)
            val dayDate = prefs.getString(WidgetUpdateWorker.KEY_DAY_DATE, null)
            val time = prefs.getString(WidgetUpdateWorker.KEY_TIME, null)

            val views = RemoteViews(context.packageName, R.layout.widget_f1)

            if (raceName != null) {
                views.setTextViewText(R.id.tv_race_name, raceName)
                views.setTextViewText(R.id.tv_session_label, sessionLabel ?: "")
                views.setTextViewText(R.id.tv_date, dayDate ?: "")
                views.setTextViewText(R.id.tv_time, time ?: "")
            } else {
                views.setTextViewText(R.id.tv_race_name, "Fetching schedule…")
                views.setTextViewText(R.id.tv_session_label, "")
                views.setTextViewText(R.id.tv_date, "")
                views.setTextViewText(R.id.tv_time, "")
            }

            appWidgetManager.updateAppWidget(appWidgetId, views)
        }

        fun schedulePeriodicUpdates(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()

            val request = PeriodicWorkRequestBuilder<WidgetUpdateWorker>(
                1, TimeUnit.HOURS
            )
                .setConstraints(constraints)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.MINUTES)
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME_PERIODIC,
                ExistingPeriodicWorkPolicy.KEEP,
                request
            )
        }

        fun enqueueImmediateUpdate(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()

            val request = OneTimeWorkRequestBuilder<WidgetUpdateWorker>()
                .setConstraints(constraints)
                .build()

            WorkManager.getInstance(context).enqueueUniqueWork(
                WORK_NAME_IMMEDIATE,
                ExistingWorkPolicy.REPLACE,
                request
            )
        }
    }
}
