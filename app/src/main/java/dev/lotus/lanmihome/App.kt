package dev.lotus.lanmihome

import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

private const val DEFAULT_URL="http://10.0.0.1:8765"
private enum class Tab { FAN, LAMP }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LanMiHomeApp() {
    val context=androidx.compose.ui.platform.LocalContext.current
    val prefs=remember{context.getSharedPreferences("lanmihome",Context.MODE_PRIVATE)}
    var baseUrl by remember{mutableStateOf(prefs.getString("base_url",DEFAULT_URL)?:DEFAULT_URL)}
    var tab by remember{mutableStateOf(Tab.FAN)}
    var fan by remember{mutableStateOf<FanState?>(null)}
    var lamp by remember{mutableStateOf<LampState?>(null)}
    var recovery by remember{mutableStateOf<RecoveryState?>(null)}
    var online by remember{mutableStateOf(false)}
    var busy by remember{mutableStateOf(false)}
    var settings by remember{mutableStateOf(false)}
    val snack=remember{SnackbarHostState()}
    val scope=rememberCoroutineScope()
    val api=remember(baseUrl){LanMiHomeApi(baseUrl)}

    suspend fun refresh(silent:Boolean=true) {
        try {
            when(tab) { Tab.FAN->fan=api.fan(); Tab.LAMP->lamp=api.lamp() }
            recovery=runCatching{api.recovery()}.getOrNull()
            online=true
        } catch(e:Exception) {
            online=false
            if(tab==Tab.FAN) fan=FanState(false,e.message) else lamp=LampState(false,e.message)
            if(!silent) snack.showSnackbar("连接失败：${e.message}")
        }
    }
    fun command(block:suspend()->Unit) { scope.launch { busy=true; try { block(); refresh() } catch(e:Exception){snack.showSnackbar("操作失败：${e.message}")} finally{busy=false} } }

    LaunchedEffect(baseUrl,tab) { while(isActive){ if(!busy) refresh(); delay(5000) } }

    Scaffold(
        snackbarHost={SnackbarHost(snack)},
        topBar={TopAppBar(title={Column{Text("LAN 米家");Text(if(online)"服务端已连接" else "服务端未连接",style=MaterialTheme.typography.labelSmall)}},actions={
            TextButton(onClick={scope.launch{refresh(false)}}){Text("刷新")}
            TextButton(onClick={settings=true}){Text("设置")}
        })},
        bottomBar={NavigationBar{
            NavigationBarItem(tab==Tab.FAN,{tab=Tab.FAN},icon={Text("◉")},label={Text("风扇")})
            NavigationBarItem(tab==Tab.LAMP,{tab=Tab.LAMP},icon={Text("●")},label={Text("台灯")})
        }}
    ){padding -> Box(Modifier.fillMaxSize().padding(padding)) {
        if(tab==Tab.FAN) FanScreen(fan,recovery,!busy,
            patch={pairs->command{api.patchFan(*pairs)}}, action={n->command{api.fanAction(n)}}, recover={command{api.forceRecovery()}})
        else LampScreen(lamp,!busy,patch={pairs->command{api.patchLamp(*pairs)}},action={n,v->command{api.lampAction(n,v)}})
        if(busy) LinearProgressIndicator(Modifier.fillMaxWidth())
    }}

    if(settings) SettingsDialog(baseUrl,{settings=false}){raw->
        runCatching{LanMiHomeApi.normalize(raw)}.onSuccess{v->prefs.edit().putString("base_url",v).apply();baseUrl=v;fan=null;lamp=null;settings=false}
            .onFailure{scope.launch{snack.showSnackbar(it.message?:"地址无效")}}
    }
}

@Composable
private fun SettingsDialog(initial:String,onDismiss:()->Unit,onSave:(String)->Unit) {
    var value by remember(initial){mutableStateOf(initial)}
    AlertDialog(onDismissRequest=onDismiss,title={Text("服务端设置")},text={Column{
        OutlinedTextField(value,{value=it},label={Text("LanMiHome 地址")},singleLine=true)
        Text("例如 http://10.0.0.1:8765\n小米 token 只保存在路由器。",style=MaterialTheme.typography.bodySmall)
    }},confirmButton={Button({onSave(value)}){Text("保存")}},dismissButton={TextButton(onDismiss){Text("取消")}})
}
