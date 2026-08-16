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
fun LampScreen(state:LampState?, enabled:Boolean, patch:(Array<Pair<String,Any>>)->Unit, action:(String,Int?)->Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement=Arrangement.spacedBy(12.dp)) {
        if(state==null) { CircularProgressIndicator(); Text("正在读取台灯…"); return@Column }
        val ok=state.available
        DeviceHeader(state.name ?: "米家台灯 2", state.ip, ok, state.error)
        Section("电源") { ToggleLine(if(state.power==true)"台灯已开启" else "台灯已关闭",state.power==true,ok&&enabled){patch(arrayOf("power" to it))} }

        var brightness by remember(state.brightness){mutableFloatStateOf((state.brightness?:50).toFloat())}
        Section("亮度 · ${brightness.roundToInt()}%") {
            Slider(brightness,{brightness=it},valueRange=1f..100f,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("brightness" to brightness.roundToInt()))})
        }

        var ct by remember(state.ct){mutableFloatStateOf((state.ct?:4000).toFloat())}
        Section("色温 · ${ct.roundToInt()} K") {
            Slider(ct,{ct=it},valueRange=2700f..5100f,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("color_temperature" to ct.roundToInt()))})
            Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.SpaceBetween){Text("2700K 暖");Text("5100K 冷")}
        }

        Section("灯光模式") {
            Row(Modifier.horizontalScroll(rememberScrollState()),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(0 to "自动",1 to "阅读",2 to "电脑",3 to "温馨",4 to "休闲",5 to "办公",6 to "娱乐").forEach { (v,n) ->
                    FilterChip(state.mode==v,{patch(arrayOf("mode" to v))},{Text(n)},enabled=ok&&enabled)
                }
            }
        }

        Section("来电默认状态") {
            Text("Default 的确切固件行为等宿舍实机验证。",style=MaterialTheme.typography.bodySmall)
            Row(Modifier.horizontalScroll(rememberScrollState()),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(0 to "默认",1 to "来电开灯",2 to "来电关灯").forEach { (v,n) ->
                    FilterChip(state.defaultPower==v,{patch(arrayOf("default_power_on_state" to v))},{Text(n)},enabled=ok&&enabled)
                }
            }
        }

        var onFade by remember(state.onFade){mutableFloatStateOf((state.onFade?:1.0).toFloat())}
        var offFade by remember(state.offFade){mutableFloatStateOf((state.offFade?:1.0).toFloat())}
        Section("开关渐变") {
            Text("开灯 ${"%.1f".format(onFade)} 秒")
            Slider(onFade,{onFade=it},valueRange=0f..5f,steps=9,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("on_gradient_seconds" to ((onFade*2).roundToInt()/2.0)))})
            Text("关灯 ${"%.1f".format(offFade)} 秒")
            Slider(offFade,{offFade=it},valueRange=0f..5f,steps=9,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("off_gradient_seconds" to ((offFade*2).roundToInt()/2.0)))})
        }

        var delay by remember(state.delayMinutes){mutableFloatStateOf((state.delayMinutes?:30).toFloat())}
        Section("延时关灯") {
            ToggleLine("启用延时",state.delayEnabled==true,ok&&enabled,state.delayRemain?.let{"剩余 $it 分钟"}){patch(arrayOf("delay_enabled" to it))}
            Text("${delay.roundToInt()} 分钟")
            Slider(delay,{delay=it},valueRange=1f..60f,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("delay_minutes" to delay.roundToInt()))})
        }

        var focus by remember(state.focusMinutes){mutableFloatStateOf((state.focusMinutes?:25).toFloat())}
        var rest by remember(state.restMinutes){mutableFloatStateOf((state.restMinutes?:5).toFloat())}
        var loops by remember(state.recycle){mutableFloatStateOf((state.recycle?:4).toFloat())}
        Section("专注模式") {
            ToggleLine("启用专注",state.focusEnabled==true,ok&&enabled){patch(arrayOf("focus_enabled" to it))}
            Text("专注 ${focus.roundToInt()} 分钟"); Slider(focus,{focus=it},valueRange=1f..90f,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("focus_minutes" to focus.roundToInt()))})
            Text("休息 ${rest.roundToInt()} 分钟"); Slider(rest,{rest=it},valueRange=1f..90f,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("rest_minutes" to rest.roundToInt()))})
            Text("循环 ${loops.roundToInt()} 次"); Slider(loops,{loops=it},valueRange=1f..60f,enabled=ok&&enabled,onValueChangeFinished={patch(arrayOf("recycle_number" to loops.roundToInt()))})
        }

        Section("快捷动作") {
            Row(Modifier.horizontalScroll(rememberScrollState()),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                OutlinedButton({action("brightness-down",10)},enabled=ok&&enabled){Text("亮度 -10")}
                OutlinedButton({action("brightness-up",10)},enabled=ok&&enabled){Text("亮度 +10")}
                OutlinedButton({action("color-temperature-down",500)},enabled=ok&&enabled){Text("色温 -500K")}
                OutlinedButton({action("color-temperature-up",500)},enabled=ok&&enabled){Text("色温 +500K")}
            }
        }
    }
}
