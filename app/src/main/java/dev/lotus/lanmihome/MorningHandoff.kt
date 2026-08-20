package dev.lotus.lanmihome

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import java.time.LocalDate
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import kotlin.math.max

internal class MorningHandoffController(
    private val context: Context,
    private val config: NightNodeConfig,
    private val executeHandoff: () -> Unit,
) {
    private var deadlineElapsed: Long? = null
    private var wifiHits = 0
    private var lastScanElapsed = 0L
    private var lastPromptDeadlineMillis: Long? = null

    private val targetSsids: Set<String> = config.handoffSsids
        .split(',')
        .map(String::trim)
        .filter(String::isNotBlank)
        .toSet()

    init {
        ensureChannel()
        NightNodeRuntime.update {
            it.copy(
                handoffState = "idle",
                handoffReason = null,
                handoffDeadlineMillis = null,
                handoffWifiHits = 0,
                handoffSeenSsids = emptyList(),
            )
        }
    }

    fun tick(rootAvailable: Boolean) {
        val nowElapsed = SystemClock.elapsedRealtime()
        val deadline = deadlineElapsed
        if (deadline != null) {
            if (nowElapsed >= deadline) {
                deadlineElapsed = null
                NightNodeRuntime.log("晨间交接倒计时结束，停止 Night Node / SoftAP")
                NightNodeRuntime.update { it.copy(handoffState = "executing") }
                cancelPromptNotification()
                executeHandoff()
                return
            }
            publishCountdown(nowElapsed)
        }

        if (NightNodeRuntime.consumeHandoffTestRequest()) {
            if (!rootAvailable) {
                NightNodeRuntime.log("晨间交接测试失败：root 不可用")
            } else {
                scanAndMaybeTrigger(nowElapsed, testMode = true)
            }
            return
        }

        if (!config.handoffEnabled || !insideWindow()) {
            wifiHits = 0
            if (deadlineElapsed == null) publishIdle()
            return
        }

        val today = LocalDate.now().toString()
        if (NightNodePrefs.cancelledDate(context) == today) {
            wifiHits = 0
            NightNodeRuntime.update { it.copy(handoffState = "cancelled", handoffReason = "本次已取消") }
            return
        }

        val snoozeUntil = NightNodePrefs.snoozeUntil(context)
        if (snoozeUntil > System.currentTimeMillis()) {
            wifiHits = 0
            deadlineElapsed = null
            NightNodeRuntime.update {
                it.copy(
                    handoffState = "delayed",
                    handoffReason = "已延后至 ${formatClock(snoozeUntil)}",
                    handoffDeadlineMillis = snoozeUntil,
                    handoffWifiHits = 0,
                    handoffSeenSsids = emptyList(),
                )
            }
            return
        } else if (snoozeUntil != 0L) {
            NightNodePrefs.setSnoozeUntil(context, 0L)
        }

        if (isCharging() && deadlineElapsed == null) {
            startOrShortenCountdown(
                seconds = config.handoffChargeDelaySeconds,
                reason = "检测到恢复充电",
                forcePrompt = true,
            )
        }

        if (rootAvailable && nowElapsed - lastScanElapsed >= 5000L) {
            scanAndMaybeTrigger(nowElapsed, testMode = false)
        }
    }

    fun executeNow() {
        NightNodeRuntime.log("用户选择立即晨间交接")
        deadlineElapsed = SystemClock.elapsedRealtime()
        NightNodeRuntime.update {
            it.copy(handoffState = "executing", handoffReason = "用户立即交接", handoffDeadlineMillis = System.currentTimeMillis())
        }
    }

    fun delayFiveMinutes() {
        val until = System.currentTimeMillis() + 5 * 60 * 1000L
        NightNodePrefs.setSnoozeUntil(context, until)
        deadlineElapsed = null
        wifiHits = 0
        cancelPromptNotification()
        NightNodeRuntime.log("晨间交接已延后 5 分钟")
        NightNodeRuntime.update {
            it.copy(
                handoffState = "delayed",
                handoffReason = "已延后至 ${formatClock(until)}",
                handoffDeadlineMillis = until,
                handoffWifiHits = 0,
                handoffSeenSsids = emptyList(),
            )
        }
    }

    fun cancelForToday() {
        val today = LocalDate.now().toString()
        NightNodePrefs.setCancelledDate(context, today)
        NightNodePrefs.setSnoozeUntil(context, 0L)
        deadlineElapsed = null
        wifiHits = 0
        cancelPromptNotification()
        NightNodeRuntime.log("已取消今天的晨间交接")
        NightNodeRuntime.update {
            it.copy(
                handoffState = "cancelled",
                handoffReason = "本次已取消",
                handoffDeadlineMillis = null,
                handoffWifiHits = 0,
                handoffSeenSsids = emptyList(),
            )
        }
    }

    fun shutdown() {
        cancelPromptNotification()
    }

    private fun scanAndMaybeTrigger(nowElapsed: Long, testMode: Boolean) {
        lastScanElapsed = nowElapsed
        val seen = NightNetwork.scanSsids(config.handoffScanInterface)
        val matched = seen.intersect(targetSsids)
        if (matched.isEmpty()) {
            wifiHits = 0
            NightNodeRuntime.update { it.copy(handoffWifiHits = 0, handoffSeenSsids = emptyList()) }
            if (testMode) NightNodeRuntime.log("晨间交接测试：未扫描到目标 SSID")
            return
        }

        wifiHits += 1
        NightNodeRuntime.update {
            it.copy(handoffWifiHits = wifiHits, handoffSeenSsids = matched.sorted())
        }
        NightNodeRuntime.log(
            "晨间 Wi-Fi 确认 $wifiHits/${config.handoffConfirmScans}：${matched.sorted().joinToString(", ")}"
        )

        if (wifiHits >= config.handoffConfirmScans) {
            wifiHits = config.handoffConfirmScans
            startOrShortenCountdown(
                seconds = config.handoffWifiDelaySeconds,
                reason = if (testMode) "测试：主路由 Wi-Fi 已确认" else "主路由 Wi-Fi 已确认",
                forcePrompt = true,
            )
        }
    }

    private fun startOrShortenCountdown(seconds: Int, reason: String, forcePrompt: Boolean) {
        val nowElapsed = SystemClock.elapsedRealtime()
        val requested = nowElapsed + seconds * 1000L
        val current = deadlineElapsed
        if (current != null && current <= requested) return

        deadlineElapsed = requested
        val deadlineWall = System.currentTimeMillis() + seconds * 1000L
        NightNodeRuntime.log("晨间交接：$reason，${seconds} 秒后执行")
        NightNodeRuntime.update {
            it.copy(
                handoffState = "countdown",
                handoffReason = reason,
                handoffDeadlineMillis = deadlineWall,
                handoffWifiHits = wifiHits,
            )
        }
        if (forcePrompt || lastPromptDeadlineMillis != deadlineWall) {
            lastPromptDeadlineMillis = deadlineWall
            showPrompt(reason, deadlineWall)
        }
    }

    private fun publishCountdown(nowElapsed: Long) {
        val deadline = deadlineElapsed ?: return
        val remain = max(0L, (deadline - nowElapsed + 999L) / 1000L)
        val wall = System.currentTimeMillis() + remain * 1000L
        NightNodeRuntime.update { state ->
            state.copy(handoffState = "countdown", handoffDeadlineMillis = wall)
        }
    }

    private fun publishIdle() {
        NightNodeRuntime.update { state ->
            if (state.handoffState == "idle") state else state.copy(
                handoffState = "idle",
                handoffReason = null,
                handoffDeadlineMillis = null,
                handoffWifiHits = 0,
                handoffSeenSsids = emptyList(),
            )
        }
    }

    private fun insideWindow(): Boolean {
        val formatter = DateTimeFormatter.ofPattern("HH:mm")
        val start = runCatching { LocalTime.parse(config.handoffWindowStart, formatter) }.getOrDefault(LocalTime.of(5, 0))
        val end = runCatching { LocalTime.parse(config.handoffWindowEnd, formatter) }.getOrDefault(LocalTime.of(6, 0))
        val now = LocalTime.now()
        return if (start <= end) now >= start && now < end else now >= start || now < end
    }

    private fun isCharging(): Boolean {
        val intent = context.registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return false
        val plugged = intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
        val status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        return plugged && (status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL)
    }

    private fun ensureChannel() {
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(HANDOFF_CHANNEL, "LAN 米家晨间交接", NotificationManager.IMPORTANCE_HIGH)
        )
    }

    private fun showPrompt(reason: String, deadlineMillis: Long) {
        val nowIntent = NightNodeService.handoffActionIntent(context, NightNodeService.ACTION_HANDOFF_NOW)
        val delayIntent = NightNodeService.handoffActionIntent(context, NightNodeService.ACTION_HANDOFF_DELAY)
        val cancelIntent = NightNodeService.handoffActionIntent(context, NightNodeService.ACTION_HANDOFF_CANCEL)

        fun pending(intent: Intent, request: Int) = PendingIntent.getService(
            context,
            request,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        val notification = Notification.Builder(context, HANDOFF_CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("LAN 米家 · 晨间交接")
            .setContentText("$reason · 默认倒计时执行")
            .setOngoing(true)
            .setOnlyAlertOnce(false)
            .addAction(Notification.Action.Builder(android.R.drawable.ic_media_play, "立即交接", pending(nowIntent, 9101)).build())
            .addAction(Notification.Action.Builder(android.R.drawable.ic_menu_recent_history, "延后 5 分钟", pending(delayIntent, 9102)).build())
            .addAction(Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel, "取消本次", pending(cancelIntent, 9103)).build())
            .build()
        context.getSystemService(NotificationManager::class.java).notify(HANDOFF_NOTIFICATION_ID, notification)

        NightNodeRuntime.update { it.copy(handoffDeadlineMillis = deadlineMillis) }
        val component = "${context.packageName}/.HandoffActivity"
        val result = RootShell.run("am start -W -n ${RootShell.quote(component)} --activity-clear-top", 8)
        if (result.code != 0) NightNodeRuntime.log("晨间交接确认界面未拉起：${result.output}")
    }

    private fun cancelPromptNotification() {
        context.getSystemService(NotificationManager::class.java).cancel(HANDOFF_NOTIFICATION_ID)
    }

    private fun formatClock(epochMillis: Long): String =
        java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US).format(java.util.Date(epochMillis))
}

class HandoffActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!BuildConfig.NIGHT_NODE_ENABLED) {
            finish()
            return
        }
        if (Build.VERSION.SDK_INT >= 27) setShowWhenLocked(true)
        window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
        setContent {
            MaterialTheme {
                HandoffPrompt(
                    onNow = {
                        NightNodeService.sendHandoffAction(this, NightNodeService.ACTION_HANDOFF_NOW)
                        finish()
                    },
                    onDelay = {
                        NightNodeService.sendHandoffAction(this, NightNodeService.ACTION_HANDOFF_DELAY)
                        finish()
                    },
                    onCancel = {
                        NightNodeService.sendHandoffAction(this, NightNodeService.ACTION_HANDOFF_CANCEL)
                        finish()
                    },
                    onFinished = { finish() },
                )
            }
        }
    }
}

@Composable
private fun HandoffPrompt(
    onNow: () -> Unit,
    onDelay: () -> Unit,
    onCancel: () -> Unit,
    onFinished: () -> Unit,
) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    val state = NightNodeRuntime.snapshot()
    val deadline = state.handoffDeadlineMillis
    val remaining = if (deadline == null) 0L else max(0L, (deadline - now + 999L) / 1000L)

    LaunchedEffect(Unit) {
        while (true) {
            delay(500)
            now = System.currentTimeMillis()
            val s = NightNodeRuntime.snapshot()
            if (s.handoffState !in setOf("countdown", "delayed")) {
                onFinished()
                break
            }
        }
    }

    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("晨间交接", style = MaterialTheme.typography.headlineMedium)
        Text(
            state.handoffReason ?: "准备切换到主路由器",
            modifier = Modifier.padding(top = 12.dp),
            style = MaterialTheme.typography.bodyLarge,
        )
        Text(
            if (state.handoffState == "delayed") "已延后" else "默认确认，$remaining 秒后自动执行",
            modifier = Modifier.padding(top = 8.dp, bottom = 24.dp),
            style = MaterialTheme.typography.bodyMedium,
        )
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onNow, modifier = Modifier.weight(1f)) { Text("立即交接") }
            OutlinedButton(onClick = onDelay, modifier = Modifier.weight(1f)) { Text("延后5分钟") }
        }
        OutlinedButton(onClick = onCancel, modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
            Text("取消本次")
        }
    }
}
