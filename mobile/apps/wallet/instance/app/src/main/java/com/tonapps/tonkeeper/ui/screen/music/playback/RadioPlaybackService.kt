package com.tonapps.tonkeeper.ui.screen.music.playback

import android.app.PendingIntent
import android.content.Intent
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService

/**
 * Фоновое воспроизведение радио: живёт, пока экран станции открыт или пока
 * пользователь не остановит эфир из уведомления. Уведомление media3 строит
 * само — из метаданных MediaItem (название станции, обложка), а кнопки
 * «назад/вперёд» появляются, потому что у плеера плейлист всех станций.
 */
@OptIn(UnstableApi::class)
class RadioPlaybackService : MediaSessionService() {

    private var mediaSession: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        val player = ExoPlayer.Builder(this)
            // Плеер сам приглушается на входящий звонок и уступает фокус другим приложениям
            .setAudioAttributes(AudioAttributes.DEFAULT, true)
            // Вынули наушники — в динамиках эфир внезапно не орёт, ставим паузу
            .setHandleAudioBecomingNoisy(true)
            .build()
        val sessionActivity = PendingIntent.getActivity(
            this,
            0,
            // Тап по уведомлению открывает приложение на вкладке «Музыка»
            Intent(Intent.ACTION_VIEW, Uri.parse("tonkeeper://music")).setPackage(packageName),
            PendingIntent.FLAG_IMMUTABLE,
        )
        mediaSession = MediaSession.Builder(this, player)
            .setSessionActivity(sessionActivity)
            .build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? {
        return mediaSession
    }

    override fun onDestroy() {
        mediaSession?.run {
            player.release()
            release()
        }
        mediaSession = null
        super.onDestroy()
    }
}

