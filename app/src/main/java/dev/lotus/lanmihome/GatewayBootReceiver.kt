package dev.lotus.lanmihome

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

class GatewayBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (BuildConfig.CLIENT_ONLY) return
        val action = intent?.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_MY_PACKAGE_REPLACED) return

        val prefs = context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
        if (!prefs.getBoolean("enabled", false)) return

        runCatching {
            val service = Intent(context, BleGatewayService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(service)
            else context.startService(service)
        }.onFailure {
            prefs.edit().putString("last_error", "自动恢复网关失败：${it.message ?: it.javaClass.simpleName}").apply()
        }
    }
}
