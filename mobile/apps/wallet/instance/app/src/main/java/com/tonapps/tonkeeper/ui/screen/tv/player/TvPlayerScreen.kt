package com.tonapps.tonkeeper.ui.screen.tv.player

import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.graphics.Color
import android.os.Bundle
import android.view.View
import android.widget.Button
import androidx.annotation.OptIn
import androidx.appcompat.widget.LinearLayoutCompat
import androidx.media3.common.AudioAttributes
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import com.tonapps.extensions.getParcelableCompat
import com.tonapps.log.L
import com.tonapps.tonkeeper.ui.base.BaseWalletScreen
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.base.ScreenContext
import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import com.tonapps.tonkeeperx.R
import uikit.base.BaseFragment
import uikit.widget.HeaderView

/**
 * Просмотр одного канала. Поток живой, поэтому позиция не сохраняется:
 * при возвращении на экран плеер встаёт на «край» эфира.
 */
@OptIn(UnstableApi::class)
class TvPlayerScreen : BaseWalletScreen<ScreenContext.None>(
    R.layout.fragment_tv_player,
    ScreenContext.None
), BaseFragment.SwipeBack {

    override val fragmentName: String = "TvPlayerScreen"

    override val viewModel: BaseWalletVM? = null

    private val channel: TvChannelEntity by lazy {
        requireArguments().getParcelableCompat(ARG_CHANNEL)!!
    }

    private var player: ExoPlayer? = null

    private lateinit var headerView: HeaderView
    private lateinit var playerView: PlayerView
    private lateinit var errorView: LinearLayoutCompat
    private lateinit var errorButton: Button

    private val playerListener = object : Player.Listener {

        override fun onPlayerError(error: PlaybackException) {
            L.e(error, "TV playback error: ${channel.name}")
            showError()
        }

        override fun onPlaybackStateChanged(playbackState: Int) {
            if (playbackState == Player.STATE_READY) {
                hideError()
            }
        }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.keepScreenOn = true

        headerView = view.findViewById(R.id.header)
        headerView.title = channel.name
        headerView.setColor(Color.TRANSPARENT)
        headerView.doOnCloseClick = { finish() }

        errorView = view.findViewById(R.id.error)
        errorButton = view.findViewById(R.id.error_button)
        errorButton.setOnClickListener { retry() }

        playerView = view.findViewById(R.id.player)
        playerView.setFullscreenButtonClickListener { fullscreen -> setFullscreen(fullscreen) }

        createPlayer()
        applyOrientation(resources.configuration.orientation)
    }

    private fun createPlayer() {
        val player = ExoPlayer.Builder(requireContext())
            // Плеер сам приглушается на входящий звонок и уступает фокус другим приложениям
            .setAudioAttributes(AudioAttributes.DEFAULT, true)
            .build()
        player.addListener(playerListener)
        player.setMediaItem(MediaItem.fromUri(channel.url))
        player.playWhenReady = true
        player.prepare()

        playerView.player = player
        this.player = player
    }

    private fun retry() {
        hideError()
        player?.let {
            it.seekToDefaultPosition()
            it.prepare()
            it.play()
        }
    }

    private fun showError() {
        errorView.visibility = View.VISIBLE
    }

    private fun hideError() {
        errorView.visibility = View.GONE
    }

    private fun setFullscreen(fullscreen: Boolean) {
        activity?.requestedOrientation = if (fullscreen) {
            ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        } else {
            ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
        }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applyOrientation(newConfig.orientation)
    }

    /**
     * В альбомной ориентации шапка убирается — иначе она закрывает часть кадра,
     * ради которой экран и разворачивали.
     */
    private fun applyOrientation(orientation: Int) {
        if (!this::headerView.isInitialized) {
            return
        }
        headerView.visibility = if (orientation == Configuration.ORIENTATION_LANDSCAPE) {
            View.GONE
        } else {
            View.VISIBLE
        }
    }

    override fun onPause() {
        super.onPause()
        player?.pause()
    }

    override fun onResume() {
        super.onResume()
        // Пока экран был скрыт, эфир ушёл вперёд — догоняем живой край
        player?.seekToDefaultPosition()
        player?.play()
    }

    override fun onDestroyView() {
        // Ориентация задана на всю активность, поэтому её нужно вернуть вручную
        activity?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        playerView.player = null
        player?.removeListener(playerListener)
        player?.release()
        player = null
        super.onDestroyView()
    }

    companion object {

        private const val ARG_CHANNEL = "channel"

        fun newInstance(channel: TvChannelEntity): TvPlayerScreen {
            val screen = TvPlayerScreen()
            screen.putParcelableArg(ARG_CHANNEL, channel)
            return screen
        }
    }
}
