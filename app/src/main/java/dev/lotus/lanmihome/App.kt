package dev.lotus.lanmihome

import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

internal const val MAIN_PREFS = "lanmihome"
internal const val DEFAULT_SERVER_URL = "http://10.0.0.1:8765"

private enum class Tab { FAN, LAMP, SENSOR, CHARGER, W96D }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LanMiHomeApp() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val prefs = remember { context.getSharedPreferences(MAIN_PREFS, Context.MODE_PRIVATE) }
    var baseUrl by remember { mutableStateOf(prefs.getString("base_url", DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL) }
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

    suspend fun loadCurrentTab() {
        val api = LanMiHomeApi(baseUrl)
        when (tab) {
            Tab.FAN -> fan = api.fan()
            Tab.LAMP -> lamp = api.lamp()
            Tab.SENSOR -> sensor = api.sensor()
            Tab.CHARGER -> chargers = api.chargers()
            Tab.W96D -> Unit
        }
        recovery = runCatching { api.recovery() }.getOrNull()
    }

    suspend fun refresh(silent: Boolean = true) {
        if (tab == Tab.W96D) return
        try {
            loadCurrentTab()
            online = true
            consecutiveRefreshFailures = 0
        } catch (e: Exception) {
            consecutiveRefreshFailures += 1
            if (!silent || consecutiveRefreshFailures >= 2) {
                online = false
                when (tab) {
                    Tab.FAN -> fan = FanState(false, e.message)
                    Tab.LAMP -> lamp = LampState(false, e.message)
                    Tab.SENSOR -> sensor = null
                    Tab.CHARGER -> chargers = emptyList()
                    Tab.W96D -> Unit
                }
            }
            if (!silent) snack.showSnackbar("连接失败：${e.message ?: "主服务端不可用"}")
        }
    }

    fun command(block: suspend (LanMiHomeApi) -> Unit) {
        scope.launch {
            busy = true
            try {
                block(LanMiHomeApi(baseUrl))
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
                                online -> "主服务端已连接"
                                else -> "主服务端未连接"
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
                Tab.W96D -> W96dScreen(primaryBase = baseUrl)
            }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
    }

    if (settings) {
        SettingsDialog(initial = baseUrl, onDismiss = { settings = false }) { raw ->
            runCatching { LanMiHomeApi.normalize(raw) }.onSuccess { value ->
                prefs.edit().putString("base_url", value).remove("auto_gateway_fallback").apply()
                baseUrl = value
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
                    "当前版本只使用这里配置的主服务端，不再自动切换到 Wi-Fi 网关或 10S Night Node。",
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
