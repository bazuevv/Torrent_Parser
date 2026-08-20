package com.tonapps.tonkeeper.ui.screen.music.player

import android.os.Bundle
import android.view.View
import android.widget.Button
import androidx.appcompat.widget.AppCompatImageView
import androidx.appcompat.widget.AppCompatTextView
import androidx.media3.common.AudioAttributes
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import com.tonapps.extensions.getParcelableCompat
import com.tonapps.log.L
import com.tonapps.tonkeeper.ui.base.BaseWalletScreen
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.base.ScreenContext
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeperx.R
import uikit.base.BaseFragment
import uikit.widget.AsyncImageView
import uikit.widget.HeaderView
import uikit.widget.LoaderView

/**
 * Экран одной радиостанции: живой поток без позиции, поэтому интерфейс —
 * только play/pause. Экран не держит дисплей включённым: музыка должна
 * играть и с погашенным экраном.
 */
class RadioPlayerScreen : BaseWalletScreen<ScreenContext.None>(
    R.layout.fragment_radio_player,
    ScreenContext.None
), BaseFragment.SwipeBack {

    override val fragmentName: String = "RadioPlayerScreen"

    override val viewModel: BaseWalletVM? = null

    private val station: RadioStationEntity by lazy {
        requireArguments().getParcelableCompat(ARG_STATION)!!
    }

    private var player: ExoPlayer? = null

    private lateinit var headerView: HeaderView
    private lateinit var logoView: AsyncImageView
    private lateinit var logoPlaceholderView: AppCompatImageView
    private lateinit var nameView: AppCompatTextView
    private lateinit var infoView: AppCompatTextView
    private lateinit var loaderView: LoaderView
    private lateinit var playButtonView: AppCompatImageView
    private lateinit var errorView: AppCompatTextView
    private lateinit var errorButtonView: Button

    private val playerListener = object : Player.Listener {

        override fun onPlaybackStateChanged(playbackState: Int) {
            loaderView.visibility = if (playbackState == Player.STATE_BUFFERING) {
                View.VISIBLE
            } else {
                View.GONE
            }
            if (playbackState == Player.STATE_READY) {
                hideError()
            }
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            applyPlayIcon(isPlaying)
        }

        override fun onPlayerError(error: PlaybackException) {
            L.e(error, "Radio playback error: ${station.name}")
            showError()
        }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.title = station.name
        headerView.doOnCloseClick = { finish() }

        logoView = view.findViewById(R.id.logo)
        logoPlaceholderView = view.findViewById(R.id.logo_placeholder)
        nameView = view.findViewById(R.id.name)
        nameView.text = station.name

        infoView = view.findViewById(R.id.info)
        infoView.text = station.info
        infoView.visibility = if (station.info.isNullOrBlank()) View.GONE else View.VISIBLE

        bindLogo()

        loaderView = view.findViewById(R.id.loader)
        playButtonView = view.findViewById(R.id.play_button)
        playButtonView.setOnClickListener { togglePlay() }

        errorView = view.findViewById(R.id.error)
        errorButtonView = view.findViewById(R.id.error_button)
        errorButtonView.setOnClickListener { retry() }

        createPlayer()
    }

    private fun bindLogo() {
        val url = station.logoUrl
        if (url.isNullOrBlank()) {
            return
        }
        logoView.visibility = View.VISIBLE
        logoPlaceholderView.visibility = View.GONE
        logoView.setImageURI(url, null)
    }

    private fun createPlayer() {
        val player = ExoPlayer.Builder(requireContext())
            // Плеер сам приглушается на входящий звонок и уступает фокус другим приложениям
            .setAudioAttributes(AudioAttributes.DEFAULT, true)
            // Вынули наушники — в динамиках эфир внезапно не орёт, ставим паузу
            .setHandleAudioBecomingNoisy(true)
            .build()
        player.addListener(playerListener)
        player.setMediaItem(MediaItem.fromUri(station.url))
        player.playWhenReady = true
        player.prepare()
        this.player = player
    }

    private fun togglePlay() {
        val player = player ?: return
        if (player.isPlaying) {
            player.pause()
        } else {
            if (player.playbackState == Player.STATE_IDLE) {
                retry()
                return
            }
            player.play()
        }
    }

    private fun retry() {
        hideError()
        player?.let {
            it.seekToDefaultPosition()
            it.prepare()
            it.play()
        }
    }

    private fun applyPlayIcon(isPlaying: Boolean) {
        playButtonView.setImageResource(
            if (isPlaying) R.drawable.ic_radio_pause_28 else R.drawable.ic_radio_play_28
        )
    }

    private fun showError() {
        errorView.visibility = View.VISIBLE
        errorButtonView.visibility = View.VISIBLE
        loaderView.visibility = View.GONE
        applyPlayIcon(false)
    }

    private fun hideError() {
        errorView.visibility = View.GONE
        errorButtonView.visibility = View.GONE
    }

    override fun onPause() {
        super.onPause()
        // Аудио могло бы играть и в фоне, но экран закрывается только вместе
        // с отказом от прослушивания — пауза здесь честнее, чем молчащий эфир
        player?.pause()
    }

    override fun onDestroyView() {
        player?.removeListener(playerListener)
        player?.release()
        player = null
        super.onDestroyView()
    }

    companion object {

        private const val ARG_STATION = "station"

        fun newInstance(station: RadioStationEntity): RadioPlayerScreen {
            val screen = RadioPlayerScreen()
            screen.putParcelableArg(ARG_STATION, station)
            return screen
        }
    }
}
