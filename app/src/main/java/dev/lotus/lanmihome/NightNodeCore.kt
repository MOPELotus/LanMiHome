package dev.lotus.lanmihome

import android.content.Context
import android.os.SystemClock
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.text.SimpleDateFormat
import java.util.Collections
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private const val NIGHT_PREFS = "lanmihome_night"
internal const val NIGHT_CHANNEL = "lanmihome-night-node"
internal const val NIGHT_NOTIFICATION_ID = 8765
internal const val HANDOFF_CHANNEL = "lanmihome-night-handoff"
internal const val HANDOFF_NOTIFICATION_ID = 8766

internal data class NightNodeConfig(
    val manageHotspot: Boolean = false,
    val ssid: String = "MiDeskLamp2_Lan",
    val passphrase: String = "",
    val interfaceName: String = "",
    val port: Int = 8765,
    val fanToken: String = "",
    val lampToken: String = "",
    val handoffEnabled: Boolean = true,
    val handoffWindowStart: String = "05:00",
    val handoffWindowEnd: String = "06:00",
    val handoffScanInterface: String = "wlan0",
    val handoffSsids: String = "LotusNet_2.4G,LotusNet_5G",
    val handoffChargeDelaySeconds: Int = 120,
    val handoffWifiDelaySeconds: Int = 15,
    val handoffConfirmScans: Int = 2,
)

internal object NightNodePrefs {
    fun read(context: Context): NightNodeConfig {
        val p = context.getSharedPreferences(NIGHT_PREFS, Context.MODE_PRIVATE)
        return NightNodeConfig(
            manageHotspot = p.getBoolean("manage_hotspot", false),
            ssid = p.getString("ssid", "MiDeskLamp2_Lan") ?: "MiDeskLamp2_Lan",
            passphrase = p.getString("passphrase", "") ?: "",
            interfaceName = p.getString("interface", "") ?: "",
            port = p.getInt("port", 8765).coerceIn(1024, 65535),
            fanToken = p.getString("fan_token", "") ?: "",
            lampToken = p.getString("lamp_token", "") ?: "",
            handoffEnabled = p.getBoolean("handoff_enabled", true),
            handoffWindowStart = p.getString("handoff_window_start", "05:00") ?: "05:00",
            handoffWindowEnd = p.getString("handoff_window_end", "06:00") ?: "06:00",
            handoffScanInterface = p.getString("handoff_scan_interface", "wlan0") ?: "wlan0",
            handoffSsids = p.getString("handoff_ssids", "LotusNet_2.4G,LotusNet_5G") ?: "LotusNet_2.4G,LotusNet_5G",
            handoffChargeDelaySeconds = p.getInt("handoff_charge_delay_seconds", 120).coerceIn(15, 1800),
            handoffWifiDelaySeconds = p.getInt("handoff_wifi_delay_seconds", 15).coerceIn(5, 300),
            handoffConfirmScans = p.getInt("handoff_confirm_scans", 2).coerceIn(1, 5),
        )
    }

    fun save(context: Context, config: NightNodeConfig) {
        context.getSharedPreferences(NIGHT_PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean("manage_hotspot", config.manageHotspot)
            .putString("ssid", config.ssid)
            .putString("passphrase", config.passphrase)
            .putString("interface", config.interfaceName)
            .putInt("port", config.port)
            .putString("fan_token", normalizeMiioToken(config.fanToken) ?: "")
            .putString("lamp_token", normalizeMiioToken(config.lampToken) ?: "")
            .putBoolean("handoff_enabled", config.handoffEnabled)
            .putString("handoff_window_start", config.handoffWindowStart)
            .putString("handoff_window_end", config.handoffWindowEnd)
            .putString("handoff_scan_interface", config.handoffScanInterface)
            .putString("handoff_ssids", config.handoffSsids)
            .putInt("handoff_charge_delay_seconds", config.handoffChargeDelaySeconds)
            .putInt("handoff_wifi_delay_seconds", config.handoffWifiDelaySeconds)
            .putInt("handoff_confirm_scans", config.handoffConfirmScans)
            .apply()
    }

    fun cancelledDate(context: Context): String =
        context.getSharedPreferences(NIGHT_PREFS, Context.MODE_PRIVATE).getString("handoff_cancelled_date", "") ?: ""

    fun setCancelledDate(context: Context, value: String) {
        context.getSharedPreferences(NIGHT_PREFS, Context.MODE_PRIVATE).edit().putString("handoff_cancelled_date", value).apply()
    }

    fun snoozeUntil(context: Context): Long =
        context.getSharedPreferences(NIGHT_PREFS, Context.MODE_PRIVATE).getLong("handoff_snooze_until", 0L)

    fun setSnoozeUntil(context: Context, value: Long) {
        context.getSharedPreferences(NIGHT_PREFS, Context.MODE_PRIVATE).edit().putLong("handoff_snooze_until", value).apply()
    }
}

internal fun normalizeMiioToken(raw: String): String? {
    val clean = raw.filter { it.isDigit() || it.lowercaseChar() in 'a'..'f' }.lowercase()
    return clean.takeIf { it.length == 32 }
}

internal data class NightNodeStatus(
    val running: Boolean = false,
    val root: Boolean = false,
    val hotspotManaged: Boolean = false,
    val hotspotInterface: String? = null,
    val hotspotAddress: String? = null,
    val serverPort: Int = 8765,
    val fanIp: String? = null,
    val lampIp: String? = null,
    val lastError: String? = null,
    val handoffState: String = "idle",
    val handoffReason: String? = null,
    val handoffDeadlineMillis: Long? = null,
    val handoffWifiHits: Int = 0,
    val handoffSeenSsids: List<String> = emptyList(),
    val logs: List<String> = emptyList(),
)

internal object NightNodeRuntime {
    private val lock = Any()
    private val logLines = java.util.ArrayDeque<String>()
    private var state = NightNodeStatus()
    private val discoverRequested = AtomicBoolean(false)
    private val handoffTestRequested = AtomicBoolean(false)

    fun snapshot(): NightNodeStatus = synchronized(lock) {
        state.copy(logs = logLines.toList())
    }

    fun update(block: (NightNodeStatus) -> NightNodeStatus) = synchronized(lock) {
        state = block(state).copy(logs = emptyList())
    }

    fun log(message: String) = synchronized(lock) {
        val stamp = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        logLines.addLast("$stamp  $message")
        while (logLines.size > 120) logLines.removeFirst()
    }

    fun requestDiscovery() {
        discoverRequested.set(true)
        log("已请求重新发现设备")
    }

    fun consumeDiscoveryRequest(): Boolean = discoverRequested.getAndSet(false)

    fun requestHandoffTest() {
        handoffTestRequested.set(true)
        log("已请求晨间交接测试")
    }

    fun consumeHandoffTestRequest(): Boolean = handoffTestRequested.getAndSet(false)
}

internal data class ShellResult(val code: Int, val output: String)

internal object RootShell {
    fun available(): Boolean = run("id -u", 5).let { it.code == 0 && it.output.trim().lineSequence().firstOrNull() == "0" }

    fun run(command: String, timeoutSeconds: Long = 12): ShellResult {
        return try {
            val process = ProcessBuilder("su", "-c", command).redirectErrorStream(true).start()
            val reader = process.inputStream.bufferedReader()
            val output = StringBuilder()
            val pump = Thread {
                runCatching { reader.forEachLine { output.appendLine(it) } }
            }.apply { isDaemon = true; start() }
            val finished = process.waitFor(timeoutSeconds, TimeUnit.SECONDS)
            if (!finished) {
                process.destroyForcibly()
                pump.join(500)
                ShellResult(124, output.toString().trim())
            } else {
                pump.join(1000)
                ShellResult(process.exitValue(), output.toString().trim())
            }
        } catch (e: Exception) {
            ShellResult(127, "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    fun quote(value: String): String = "'" + value.replace("'", "'\\''") + "'"
}

internal data class HotspotInfo(
    val interfaceName: String,
    val address: Inet4Address,
    val broadcast: Inet4Address?,
)

internal object NightNetwork {
    fun detect(preferred: String): HotspotInfo? {
        val interfaces = runCatching { Collections.list(NetworkInterface.getNetworkInterfaces()) }.getOrDefault(emptyList())
        val candidates = interfaces.filter { iface ->
            runCatching { iface.isUp }.getOrDefault(false) &&
                !iface.isLoopback &&
                (preferred.isBlank() || iface.name == preferred)
        }.flatMap { iface ->
            iface.interfaceAddresses.mapNotNull { ia ->
                val addr = ia.address as? Inet4Address ?: return@mapNotNull null
                if (!addr.isSiteLocalAddress) return@mapNotNull null
                HotspotInfo(iface.name, addr, ia.broadcast as? Inet4Address)
            }
        }

        if (preferred.isNotBlank()) return candidates.firstOrNull()
        return candidates.firstOrNull { it.interfaceName.matches(Regex("^(wlan|ap|swlan|softap).*", RegexOption.IGNORE_CASE)) }
            ?: candidates.firstOrNull { !it.interfaceName.startsWith("rmnet") && !it.interfaceName.startsWith("tun") }
    }

    fun startHotspot(config: NightNodeConfig): HotspotInfo? {
        detect(config.interfaceName)?.let { return it }
        if (!config.manageHotspot) return null
        if (config.ssid.isBlank() || config.passphrase.length < 8) {
            NightNodeRuntime.log("热点参数不完整：SSID 不能为空，WPA2 密码至少 8 位")
            return null
        }
        val args = "${RootShell.quote(config.ssid)} wpa2 ${RootShell.quote(config.passphrase)} -b 2"
        val softap = RootShell.run("cmd wifi start-softap $args", 15)
        SystemClock.sleep(1500)
        detect(config.interfaceName)?.let {
            NightNodeRuntime.log("已通过 SoftAP 启动 2.4 GHz WPA2 热点：${config.ssid}")
            return it
        }

        val lohs = RootShell.run("cmd wifi start-lohs $args", 15)
        SystemClock.sleep(1200)
        val info = detect(config.interfaceName)
        if (info != null) {
            NightNodeRuntime.log("已通过 LocalOnlyHotspot 启动 2.4 GHz WPA2 热点：${config.ssid}")
        } else {
            val detail = listOf(softap, lohs).joinToString(" | ") { "${it.code}:${it.output.ifBlank { "无输出" }}" }
            NightNodeRuntime.log("启动热点失败：$detail")
        }
        return info
    }

    fun stopHotspot(config: NightNodeConfig) {
        if (!config.manageHotspot) return
        val lohs = RootShell.run("cmd wifi stop-lohs", 8)
        val softap = RootShell.run("cmd wifi stop-softap", 8)
        val ok = lohs.code == 0 || softap.code == 0
        NightNodeRuntime.log(if (ok) "已停止 App 管理的热点" else "停止热点失败：${lohs.output} | ${softap.output}")
    }

    fun neighborAddresses(info: HotspotInfo): Set<Inet4Address> {
        val result = RootShell.run("ip -4 neigh show dev ${RootShell.quote(info.interfaceName)}", 5)
        if (result.code != 0) return emptySet()
        val regex = Regex("^(\\d{1,3}(?:\\.\\d{1,3}){3})\\s", RegexOption.MULTILINE)
        return regex.findAll(result.output).mapNotNull { match ->
            runCatching { InetAddress.getByName(match.groupValues[1]) as? Inet4Address }.getOrNull()
        }.toSet()
    }

    fun scanSsids(interfaceName: String): Set<String> {
        if (interfaceName.isBlank()) return emptySet()
        val command = "iw dev ${RootShell.quote(interfaceName)} scan 2>/dev/null | sed -n 's/^[[:space:]]*SSID: //p'"
        val result = RootShell.run(command, 12)
        if (result.code != 0) return emptySet()
        return result.output.lineSequence().map(String::trim).filter(String::isNotBlank).toSet()
    }
}
