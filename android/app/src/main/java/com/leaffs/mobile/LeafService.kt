package com.leaffs.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import com.chaquo.python.Python

/**
 * 前台服务：让 LeafFS 在退到后台后继续共享。
 *
 * Python 服务线程运行在 App 进程内，只要进程存活服务就在。普通后台进程会被系统
 * 很快回收，前台服务 + 常驻通知是保活的标准做法（用户可见、可感知）。
 */
class LeafService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 通知栏的「关闭应用」
        if (intent?.action == ACTION_CLOSE) {
            closeApp()
            return START_NOT_STICKY
        }
        // 转为前台必须及时（否则系统在约 5 秒后抛异常）
        goForeground()
        // 幂等确保 Python 服务在跑（Activity 已启动过则 start() 直接返回 already）
        Thread {
            try {
                Python.getInstance().getModule("leaffs_mobile").callAttr("start")
            } catch (_: Throwable) {
                // 启动失败由页面/日志呈现，不在这里打断前台状态
            }
        }.start()
        return START_STICKY
    }

    /** 「关闭应用」的 PendingIntent：回到这个 Service 并带上关闭 action */
    private fun closeIntent(): PendingIntent = PendingIntent.getService(
        this, 2,
        Intent(this, LeafService::class.java).setAction(ACTION_CLOSE),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    /**
     * 关闭应用：停前台服务（通知一起消失）并结束进程。
     *
     * 顺序讲究和 MainActivity.exitCompletely 一致：先停服务、稍候再结束进程，
     * 否则 system_server 还没处理完 stopSelf，会按 START_STICKY 把服务又拉起来。
     */
    private fun closeApp() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        // 先把这个任务从最近任务里摘掉再杀进程：否则前台 Activity 的进程被杀后，
        // 系统会把它当成"待恢复的任务"，应用立刻又被拉起来
        //（MainActivity.exitCompletely 也是这个顺序）
        (getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager)
            .appTasks.firstOrNull()?.finishAndRemoveTask()
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            android.os.Process.killProcess(android.os.Process.myPid())
        }, 500)
    }

    private fun ensureChannel() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        // NotificationChannel 自 API 26 起必需，本项目 minSdk 26，无需版本判断
        val ch = NotificationChannel(CHANNEL_ID, "LeafFS 共享", NotificationManager.IMPORTANCE_LOW)
        ch.description = "共享服务运行状态"
        ch.setShowBadge(false)
        nm.createNotificationChannel(ch)
        // 事件提醒（后台保护失效等）：要用户注意到，用默认重要级（会响一声）
        val alert = NotificationChannel(
            ALERT_CHANNEL_ID, "LeafFS 提醒", NotificationManager.IMPORTANCE_DEFAULT)
        alert.description = "共享中断等需要处理的事件"
        nm.createNotificationChannel(alert)
    }

    private fun goForeground() {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notif = Notification.Builder(this, CHANNEL_ID)
            // 叶子剪影小图标 + 主题色，与网页视觉一致
            .setSmallIcon(R.drawable.ic_stat_leaf)
            .setColor(getColor(R.color.lf_primary))
            .setContentTitle("LeafFS 正在共享")
            .setContentText("局域网内设备可访问，点按返回应用")
            .setContentIntent(pi)
            .setOngoing(true)
            // 通知栏直接关闭：Python 服务跑在进程里，只能靠结束进程真正停掉共享
            .addAction(0, "关闭应用", closeIntent())
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 15 起 dataSync 有累计时长上限（约 6 小时）会被系统停掉，
            // 长期驻留的共享服务改用 specialUse（用途在 Manifest 的 property 里声明）
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    /**
     * Android 15（API 35）起前台服务超时回调 —— 不实现会被系统判成 ANR。
     * 改用 specialUse 后理论上不会走到这里；真被限额时先提醒用户，再停掉自己。
     *
     * 注意：停的只是这个前台服务，Python 服务还在进程里跑、局域网共享仍然可用；
     * 真正的影响是失去前台保护，进程随时可能被系统回收。
     */
    @Suppress("NewApi")
    override fun onTimeout(startId: Int, fgsType: Int) {
        notifyProtectionLost()
        stopSelf()
    }

    /** 后台保护失效：发一条会响、可点的通知，点一下回应用即可恢复（见 MainActivity.onNewIntent） */
    private fun notifyProtectionLost() {
        val pi = PendingIntent.getActivity(
            this, 1, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notif = Notification.Builder(this, ALERT_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_leaf)
            .setColor(getColor(R.color.lf_primary))
            .setContentTitle("LeafFS 后台保护已失效")
            .setContentText("系统限制了后台服务时长，共享可能随时中断 —— 点按恢复")
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(ALERT_NOTIF_ID, notif)
    }

    companion object {
        private const val CHANNEL_ID = "leaffs_share"
        private const val ALERT_CHANNEL_ID = "leaffs_alert"
        private const val NOTIF_ID = 1001
        private const val ACTION_CLOSE = "com.leaffs.mobile.action.CLOSE"
        private const val ALERT_NOTIF_ID = 1002

        /** 启动/确保前台服务在跑（Activity 启动时调用） */
        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, LeafService::class.java))
        }

        /** 停止前台服务（"彻底退出"时调用）：通知消失，进程随后结束 */
        fun stop(ctx: Context) {
            try {
                ctx.stopService(Intent(ctx, LeafService::class.java))
            } catch (_: Exception) {
            }
        }
    }
}
