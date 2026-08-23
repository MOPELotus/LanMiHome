package dev.lotus.lanmihome

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.net.Inet4Address
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex

internal const val MAIN_PREFS = "lanmihome"
internal const val DEFAULT_SERVER_URL = "http://10.0.0.1:8765"

private enum class Tab { FAN, LAMP, SENSOR, CHARGER, W96D }
private enum class BackendKind { PRIMARY, WIFI_GATEWAY }

private data class BackendTarget(
    val url: String,
    val kind: BackendKind,
    val network: Network? = null,
)

private fun gatewayServerTarget(context: Context, port: Int = 8765): BackendTarget? {
    val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
    val networks = cm.allNetworks.toList().sortedByDescending { it == cm.activeNetwork }
    for (network in networks) {
        val caps = cm.getNetworkCapabilities(network) ?: continue
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
        val props = cm.getLinkProperties(network) ?: continue
        val gateway = props.routes.asSequence()
            .filter { it.isDefaultRoute }
            .mapNotNull { it.gateway as? Inet4Address }
            .firstOrNull() ?: continue
        return BackendTarget(
            url = "http://${gateway.hostAddress}:$port",
            kind = BackendKind.WIFI_GATEWAY,
            network = network,
        )
    }
    return null
}

private suspend fun <T> withTargetNetwork(
    context: Context,
    target: BackendTarget,
    mutex: Mutex,
    block: suspend () -> T,
): T {
    mutex.lock()
    try {
        val network = target.network ?: return block()
        val cm = context.getSystemService(ConnectivityManager::class.java)
            ?: throw ApiException("无法取得 ConnectivityManager")
        val previous = cm.boundNetworkForProcess
        if (!cm.bindProcessToNetwork(network)) throw ApiException("无法绑定当前 Wi-Fi 网络")
        try {
            return block()
        } finally {
            cm.bindProcessToNetwork(previous)
        }
    } finally {
        mutex.unlock()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LanMiHomeApp() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val prefs = remember { context.getSharedPreferences(MAIN_PREFS, Context.MODE_PRIVATE) }
    var baseUrl by remember { mutableStateOf(prefs.getString("base_url", DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL) }
    var activeBase by remember { mutableStateOf(baseUrl) }
    var activeKind by remember { mutableStateOf(BackendKind.PRIMARY) }
    var tab by remember { mutableStateOf(Tab.FAN) }
    var fan by remember { mutableStateOf<FanState?>(null) }
    var lamp by remember { mutableStateOf<LampState?>(null) }
    var sensor by remember { mutableStateOf<SensorState?>(null) }
    var chargers by remember { mutableStateOf<List<ChargerState>?>(null) }
    var recovery by remember { mutableStateOf<RecoveryState?>(null) }
    var online by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var settings by remember { mutableStateOf(false) }
    var consecutiveRefreshFailures by remember { mutableIntStateOf(0) }
    val snack = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val networkMutex = remember { Mutex() }

    fun resolvedActiveTarget(): BackendTarget? = when (activeKind) {
        BackendKind.PRIMARY -> BackendTarget(activeBase, BackendKind.PRIMARY)
        BackendKind.WIFI_GATEWAY -> gatewayServerTarget(context)?.takeIf { it.url == activeBase }
    }

    fun candidateTargets(): List<BackendTarget> {
        val gateway = gatewayServerTarget(context)
        return listOfNotNull(
            resolvedActiveTarget(),
            BackendTarget(baseUrl, BackendKind.PRIMARY),
            gateway,
        ).distinctBy { "${it.kind}:${it.url}" }
    }

    suspend fun loadCurrentTab(target: BackendTarget) {
        withTargetNetwork(context, target, networkMutex) {
            val api = LanMiHomeApi(target.url)
            when (tab) {
                Tab.FAN -> fan = api.fan()
                Tab.LAMP -> lamp = api.lamp()
                Tab.SENSOR -> sensor = api.sensor()
                Tab.CHARGER -> chargers = api.chargers()
                Tab.W96D -> Unit
            }
            recovery = runCatching { api.recovery() }.getOrNull()
        }
    }

    suspend fun refresh(silent: Boolean = true) {
        if (tab == Tab.W96D) return
        var lastError: Exception? = null
        for (target in candidateTargets()) {
            try {
                loadCurrentTab(target)
                activeBase = target.url
                activeKind = target.kind
                online = true
                consecutiveRefreshFailures = 0
                return
            } catch (e: Exception) {
                lastError = e
            }
        }
        consecutiveRefreshFailures += 1
        if (!silent || consecutiveRefreshFailures >= 2) {
            online = false
            when (tab) {
                Tab.FAN -> fan = FanState(false, lastError?.message)
                Tab.LAMP -> lamp = LampState(false, lastError?.message)
                Tab.SENSOR -> sensor = null
                Tab.CHARGER -> chargers = emptyList()
                Tab.W96D -> Unit
            }
        }
        if (!silent) snack.showSnackbar("连接失败：${lastError?.message ?: "无可用路由器服务端"}")
    }

    fun command(block: suspend (LanMiHomeApi) -> Unit) {
        scope.launch {
            busy = true
            try {
                if (!online) refresh(silent = true)
                val target = resolvedActiveTarget()
                    ?: throw ApiException("当前路由器服务端不可用，请点击刷新")
                withTargetNetwork(context, target, networkMutex) {
                    block(LanMiHomeApi(target.url))
                }
                refresh()
            } catch (e: Exception) {
                snack.showSnackbar("操作失败：${e.message}")
            } finally {
                busy = false
            }
        }
    }

    LaunchedEffect(baseUrl, tab) {
        if (tab == Tab.W96D) return@LaunchedEffect
        while (isActive) {
            if (!busy) refresh()
            delay(if (tab == Tab.CHARGER) 3000 else 5000)
        }
    }

    val routerBaseForW96d = if (online) {
        resolvedActiveTarget()?.url ?: baseUrl
    } else {
        gatewayServerTarget(context)?.url ?: baseUrl
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snack) },
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("LAN 米家")
                        Text(
                            when {
                                tab == Tab.W96D -> "W96D · 路由器 BLE / 本机 BLE"
                                online && activeKind == BackendKind.PRIMARY -> "主服务端已连接"
                                online -> "当前 Wi-Fi 路由器已连接 · $activeBase"
                                else -> "服务端未连接"
                            },
                            style = MaterialTheme.typography.labelSmall,
                        )
                    }
                },
                actions = {
                    if (tab != Tab.W96D) {
                        TextButton(
                            enabled = !busy,
                            onClick = {
                                scope.launch {
                                    busy = true
                                    try { refresh(silent = false) }
                                    finally { busy = false }
                                }
                            },
                        ) { Text("刷新") }
                    }
                    TextButton(onClick = { settings = true }) { Text("设置") }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                NavigationBarItem(tab == Tab.FAN, { tab = Tab.FAN }, icon = { Text("◉") }, label = { Text("风扇") })
                NavigationBarItem(tab == Tab.LAMP, { tab = Tab.LAMP }, icon = { Text("●") }, label = { Text("台灯") })
                NavigationBarItem(tab == Tab.SENSOR, { tab = Tab.SENSOR }, icon = { Text("⌁") }, label = { Text("温湿度") })
                NavigationBarItem(tab == Tab.CHARGER, { tab = Tab.CHARGER }, icon = { Text("⚡") }, label = { Text("充电头") })
                NavigationBarItem(tab == Tab.W96D, { tab = Tab.W96D }, icon = { Text("◎") }, label = { Text("W96D") })
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (tab) {
                Tab.FAN -> FanScreen(
                    fan, recovery, !busy,
                    patch = { pairs -> command { it.patchFan(*pairs) } },
                    action = { n -> command { it.fanAction(n) } },
                    recover = { command { it.forceRecovery() } },
                )
                Tab.LAMP -> LampScreen(
                    lamp, !busy,
                    patch = { pairs -> command { it.patchLamp(*pairs) } },
                    action = { n, v -> command { it.lampAction(n, v) } },
                )
                Tab.SENSOR -> ClientSensorScreen(sensor)
                Tab.CHARGER -> ChargerScreen(
                    chargers = chargers,
                    enabled = !busy,
                    patch = { name, pairs -> command { it.patchCharger(name, *pairs) } },
                    setPort = { name, port, on -> command { it.setChargerPort(name, port, on) } },
                    setProtocol = { name, port, protocol, on -> command { it.setChargerProtocol(name, port, protocol, on) } },
                    setTimer = { name, port, minutes -> command { it.setChargerTimer(name, port, minutes) } },
                )
                Tab.W96D -> W96dScreen(primaryBase = routerBaseForW96d)
            }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
    }

    if (settings) {
        SettingsDialog(initial = baseUrl, onDismiss = { settings = false }) { raw ->
            runCatching { LanMiHomeApi.normalize(raw) }.onSuccess { value ->
                prefs.edit().putString("base_url", value).remove("auto_gateway_fallback").apply()
                baseUrl = value
                activeBase = value
                activeKind = BackendKind.PRIMARY
                online = false
                consecutiveRefreshFailures = 0
                fan = null
                lamp = null
                sensor = null
                chargers = null
                settings = false
            }.onFailure { scope.launch { snack.showSnackbar(it.message ?: "地址无效") } }
        }
    }
}

@Composable
private fun SettingsDialog(initial: String, onDismiss: () -> Unit, onSave: (String) -> Unit) {
    var value by remember(initial) { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("服务端设置") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(value, { value = it }, label = { Text("主 LanMiHome 地址") }, singleLine = true)
                Text(
                    "会优先使用已连接的路由器服务端；配置地址不可用时，仅尝试当前 Wi-Fi 默认网关 :8765。不会启用或探测 10S Night Node。",
                    style = MaterialTheme.typography.bodySmall,
                )
                Text(
                    "W96D 在 HOME / SCHOOL 均由路由器持续持有 BLE；只有 OUTDOOR 会先释放路由器，再由本机 BluetoothGatt 接管。",
                    style = MaterialTheme.typography.bodySmall,
                )
                Text(
                    "例如 http://10.0.0.1:8765。Xiaomi/BLE/CUKTECH secrets 继续只保存在路由器本地配置。",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        },
        confirmButton = { Button({ onSave(value) }) { Text("保存") } },
        dismissButton = { TextButton(onDismiss) { Text("取消") } },
    )
}
