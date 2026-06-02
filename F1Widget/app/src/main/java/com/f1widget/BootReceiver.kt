package com.f1widget

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            F1WidgetProvider.schedulePeriodicUpdates(context)
            F1WidgetProvider.enqueueImmediateUpdate(context)
        }
    }
}
