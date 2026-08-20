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
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import java.net.Inet4Address

internal const val MAIN_PREFS = "lanmihome"
internal const val DEFAULT_SERVER_URL = "http://10.0.0.1:8765"

private enum class Tab { FAN, LAMP, SENSOR, CHARGER, NIGHT }
private enum class BackendKind { PRIMARY, WIFI_GATEWAY, LOCAL }

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
        val previous = ConnectivityManager.getBoundNetworkForProcess()
        if (!cm.bindProcessToNetwork(network)) {
            throw ApiException("无法绑定当前 Wi-Fi 网络")
        }
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
    var gatewayFallback by remember { mutableStateOf(prefs.getBoolean("auto_gateway_fallback", true)) }
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
        BackendKind.LOCAL -> BackendTarget(activeBase, BackendKind.LOCAL)
        BackendKind.WIFI_GATEWAY -> gatewayServerTarget(context)?.takeIf { it.url == activeBase }
    }

    fun rediscoveryTargets(): List<BackendTarget> {
        val targets = mutableListOf<BackendTarget>()

        if (activeKind == BackendKind.LOCAL) {
            targets += BackendTarget(activeBase, BackendKind.LOCAL)
        } else {
            resolvedActiveTarget()?.let { targets += it }
        }

        targets += BackendTarget(baseUrl, BackendKind.PRIMARY)
        if (gatewayFallback) {
            gatewayServerTarget(context)?.let { targets += it }
        }

        return targets.distinctBy { "${it.kind}:${it.url}" }
    }

    suspend fun loadCurrentTab(target: BackendTarget) {
        withTargetNetwork(context, target, networkMutex) {
            val api = LanMiHomeApi(target.url)
            when (tab) {
                Tab.FAN -> fan = api.fan()
                Tab.LAMP -> lamp = api.lamp()
                Tab.SENSOR -> sensor = api.sensor()
                Tab.CHARGER -> chargers = api.chargers()
                Tab.NIGHT -> Unit
            }
            recovery = runCatching { api.recovery() }.getOrNull()
        }
    }

    suspend fun refresh(
        silent: Boolean = true,
        rediscoverBackend: Boolean = false,
    ) {
        if (tab == Tab.NIGHT) return

        val targets = if (rediscoverBackend) {
            rediscoveryTargets()
        } else {
            listOfNotNull(resolvedActiveTarget())
        }

        var lastError: Exception? = null
        for (target in targets) {
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
        if (!silent || consecutiveRefreshFailures >= 2 || targets.isEmpty()) {
            online = false
            when (tab) {
                Tab.FAN -> fan = FanState(false, lastError?.message)
                Tab.LAMP -> lamp = LampState(false, lastError?.message)
                Tab.SENSOR -> sensor = null
                Tab.CHARGER -> chargers = emptyList()
                Tab.NIGHT -> Unit
            }
        }
        if (!silent) {
            snack.showSnackbar("连接失败：${lastError?.message ?: "无可用服务端"}")
        }
    }

    fun command(block: suspend (LanMiHomeApi) -> Unit) {
        scope.launch {
            busy = true
            try {
                val target = resolvedActiveTarget()
                    ?: throw ApiException("当前备用 Wi-Fi 已断开，请点击刷新重新发现")
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
        if (tab == Tab.NIGHT) return@LaunchedEffect
        while (isActive) {
            if (!busy) refresh()
            delay(if (tab == Tab.CHARGER) 3000 else 5000)
        }
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
                                tab == Tab.NIGHT -> "夜间节点管理"
                                online && activeBase == baseUrl -> "主服务端已连接"
                                online && activeKind == BackendKind.LOCAL -> "本机 Night Node · $activeBase"
                                online -> "备用节点已连接 · $activeBase"
                                else -> "服务端未连接"
                            },
                            style = MaterialTheme.typography.labelSmall,
                        )
                    }
                },
                actions = {
                    if (tab != Tab.NIGHT) {
                        TextButton(
                            enabled = !busy,
                            onClick = {
                                scope.launch {
                                    busy = true
                                    try {
                                        refresh(silent = false, rediscoverBackend = true)
                                    } finally {
                                        busy = false
                                    }
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
                if (BuildConfig.NIGHT_NODE_ENABLED) {
                    NavigationBarItem(tab == Tab.NIGHT, { tab = Tab.NIGHT }, icon = { Text("☾") }, label = { Text("夜间") })
                }
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (tab) {
                Tab.FAN -> FanScreen(
                    fan,
                    recovery,
                    !busy,
                    patch = { pairs -> command { it.patchFan(*pairs) } },
                    action = { n -> command { it.fanAction(n) } },
                    recover = { command { it.forceRecovery() } },
                )
                Tab.LAMP -> LampScreen(
                    lamp,
                    !busy,
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
                Tab.NIGHT -> NightNodeScreen(
                    onUseLocal = {
                        activeBase = "http://127.0.0.1:${NightNodePrefs.read(context).port}"
                        activeKind = BackendKind.LOCAL
                        online = true
                        consecutiveRefreshFailures = 0
                        fan = null
                        lamp = null
                        tab = Tab.FAN
                    }
                )
            }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
    }

    if (settings) {
        SettingsDialog(
            initial = baseUrl,
            initialFallback = gatewayFallback,
            onDismiss = { settings = false },
        ) { raw, fallback ->
            runCatching { LanMiHomeApi.normalize(raw) }.onSuccess { value ->
                prefs.edit()
                    .putString("base_url", value)
                    .putBoolean("auto_gateway_fallback", fallback)
                    .apply()
                baseUrl = value
                gatewayFallback = fallback
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
private fun SettingsDialog(
    initial: String,
    initialFallback: Boolean,
    onDismiss: () -> Unit,
    onSave: (String, Boolean) -> Unit,
) {
    var value by remember(initial) { mutableStateOf(initial) }
    var fallback by remember(initialFallback) { mutableStateOf(initialFallback) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("服务端设置") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(value, { value = it }, label = { Text("主 LanMiHome 地址") }, singleLine = true)
                ToggleLine(
                    "刷新时探测当前 Wi-Fi 网关作为备用节点",
                    fallback,
                    note = "后台刷新只沿用当前已选服务端；只有点击“刷新”时才重新探测主服务端和 http://<当前 Wi-Fi 网关>:8765。备用节点请求会强制走 Wi-Fi，避免无互联网热点下被移动数据抢走。",
                ) { fallback = it }
                Text(
                    "例如 http://10.0.0.1:8765\n主路由器上的 Xiaomi/BLE/CUKTECH secrets 仍只保存在路由器；Night Node 使用自己的本地私有配置。",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        },
        confirmButton = { Button({ onSave(value, fallback) }) { Text("保存") } },
        dismissButton = { TextButton(onDismiss) { Text("取消") } },
    )
}
