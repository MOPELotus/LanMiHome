package dev.lotus.lanmihome

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

@Composable
fun FanScreen(state:FanState?, recovery:RecoveryState?, enabled:Boolean, patch:(Array<Pair<String,Any>>)->Unit, action:(String)->Unit, recover:()->Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement=Arrangement.spacedBy(12.dp)) {
        if(state==null) { CircularProgressIndicator(); Text("正在读取风扇…"); return@Column }
        val ok=state.available
        DeviceHeader(state.name ?: "米家直流变频落地扇 1X", state.ip, ok, state.error)

        Section("电源") {
            ToggleLine(if(state.power==true) "风扇已开启" else "风扇已关闭", state.power==true, ok&&enabled,
                "使用 p5c 实机验证的读状态 + toggle 幂等逻辑") { patch(arrayOf("power" to it)) }
        }

        var speed by remember(state.speed) { mutableFloatStateOf((state.speed ?: 1).toFloat()) }
        Section("无级风速 · ${speed.roundToInt()}") {
            Slider(speed, {speed=it}, valueRange=1f..100f, enabled=ok&&enabled,
                onValueChangeFinished={patch(arrayOf("speed" to speed.roundToInt()))})
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(1 to "1档",35 to "2档",70 to "3档",100 to "4档").forEach { (v,n) ->
                    AssistChip(onClick={patch(arrayOf("speed" to v))}, enabled=ok&&enabled, label={Text("$n · $v")})
                }
            }
        }

        Section("风型") {
            Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                FilterChip(state.mode==0,{patch(arrayOf("mode" to "straight"))},{Text("直吹风")},enabled=ok&&enabled)
                FilterChip(state.mode==1,{patch(arrayOf("mode" to "natural"))},{Text("自然风")},enabled=ok&&enabled)
            }
        }

        Section("摆头") {
            ToggleLine("左右摆头", state.swing==true, ok&&enabled) { patch(arrayOf("swing" to it)) }
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(30,60,90,120,140).forEach { a ->
                    FilterChip(state.swingAngle==a,{patch(arrayOf("swing_angle" to a))},{Text("$a°")},enabled=ok&&enabled)
                }
            }
            Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                OutlinedButton({action("turn-left")},Modifier.weight(1f),enabled=ok&&enabled){Text("← 微调")}
                OutlinedButton({action("turn-right")},Modifier.weight(1f),enabled=ok&&enabled){Text("微调 →")}
            }
        }

        var timer by remember(state.offDelay) { mutableFloatStateOf((state.offDelay ?: 0).toFloat()) }
        Section("延时关机 · ${timer.roundToInt()} 分钟") {
            Slider(timer,{timer=it},valueRange=0f..480f,enabled=ok&&enabled,
                onValueChangeFinished={patch(arrayOf("off_delay_minutes" to timer.roundToInt()))})
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(0,30,60,120,240,480).forEach { m -> AssistChip({patch(arrayOf("off_delay_minutes" to m))},label={Text(if(m==0)"关闭" else "${m}分")},enabled=ok&&enabled) }
            }
        }

        Section("设备设置") {
            ToggleLine("指示灯",state.indicator==true,ok&&enabled){patch(arrayOf("indicator" to it))}
            HorizontalDivider()
            ToggleLine("操作提示音",state.alarm==true,ok&&enabled){patch(arrayOf("alarm" to it))}
            HorizontalDivider()
            ToggleLine("童锁 / 实体按键锁",state.childLock==true,ok&&enabled){patch(arrayOf("child_lock" to it))}
        }

        Section("来电恢复") {
            Text(when { recovery==null->"尚未读取"; recovery.active->"正在重试 · ${recovery.attempts} 次"; recovery.success->"最近恢复成功"; else->recovery.reason ?: "未运行" })
            recovery?.error?.let { Text(it,color=MaterialTheme.colorScheme.error) }
            Text("自动恢复只在路由器于 05:00–06:30 启动时触发。",style=MaterialTheme.typography.bodySmall)
            OutlinedButton(recover,enabled=enabled){Text("强制测试拉起")}
        }
    }
}
