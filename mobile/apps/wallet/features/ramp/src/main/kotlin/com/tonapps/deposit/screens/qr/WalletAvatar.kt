package com.tonapps.deposit.screens.qr

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import coil3.compose.AsyncImage
import com.tonapps.emoji.ui.EmojiView
import java.io.File

private const val EMOJI_TO_CIRCLE_RATIO = 0.7f
private val PHOTO_INSET = 3.dp

/**
 * Аватар кошелька: эмодзи из его метки на круге цвета метки.
 *
 * Рисуется через [EmojiView] из lib/emoji, а не текстом, потому что метка может
 * содержать не только обычное эмодзи, но и один из встроенных значков
 * (`custom_wallet`, `custom_leaf` и прочие) — их умеет разворачивать только он.
 *
 * Эмодзи занимает 0.7 диаметра круга. В списке кошельков пропорция мельче
 * (24 dp внутри 44 dp), но там аватар стоит в плотной строке; здесь он крупный
 * и одиночный, поэтому эмодзи заполняет круг заметнее.
 *
 * Если для кошелька выбрана фотография, вместо эмодзи показывается она —
 * с небольшим отступом внутри круга, чтобы цвет метки был виден кольцом.
 *
 * @param emoji значение [com.tonapps.blockchain.model.legacy.Wallet.Label.emoji]
 * @param color цвет метки, ARGB-число из [com.tonapps.blockchain.model.legacy.Wallet.Label.color]
 * @param photoPath путь к фотографии либо null, когда показывается эмодзи
 */
@Composable
fun WalletAvatar(
    emoji: CharSequence,
    color: Int,
    modifier: Modifier = Modifier,
    size: Dp = 44.dp,
    photoPath: String? = null,
) {
    Box(
        modifier = modifier
            .size(size)
            .background(color = Color(color), shape = CircleShape),
        contentAlignment = Alignment.Center
    ) {
        if (photoPath.isNullOrBlank()) {
            AndroidView(
                modifier = Modifier.size(size * EMOJI_TO_CIRCLE_RATIO),
                factory = { context -> EmojiView(context) },
                update = { view ->
                    view.setEmoji(emoji, android.graphics.Color.TRANSPARENT)
                }
            )
        } else {
            AsyncImage(
                model = File(photoPath),
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .fillMaxSize()
                    .padding(PHOTO_INSET)
                    .clip(CircleShape)
            )
        }
    }
}
