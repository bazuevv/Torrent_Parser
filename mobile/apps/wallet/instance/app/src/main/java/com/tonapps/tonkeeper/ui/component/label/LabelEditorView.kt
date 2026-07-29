package com.tonapps.tonkeeper.ui.component.label

import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Color
import android.util.AttributeSet
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.ImageView
import android.widget.TextView
import androidx.core.view.WindowInsetsCompat
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.tonapps.emoji.Emoji
import com.tonapps.emoji.ui.EmojiView
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.color.backgroundPageColor
import com.tonapps.uikit.color.stateList
import com.tonapps.uikit.list.LinearLayoutManager
import com.tonapps.wallet.localization.Localization
import com.tonapps.blockchain.model.legacy.WalletColor
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import uikit.HapticHelper
import uikit.extensions.dp
import uikit.extensions.runAnimation
import uikit.extensions.useAttributes
import uikit.extensions.withAlpha
import uikit.widget.ColumnLayout
import uikit.widget.InputView

class LabelEditorView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0
) : ColumnLayout(context, attrs, defStyle) {

    var doOnDone: ((name: String, emoji: String, color: Int) -> Unit)? = null
    var doOnChange: ((name: String, emoji: String, color: Int) -> Unit)? = null
    var doOnPickPhoto: (() -> Unit)? = null
    var doOnRemovePhoto: (() -> Unit)? = null

    private val colorAdapter = ColorAdapter {
        color = it
    }

    private val emojiAdapter = EmojiAdapter {
        emoji = it.value
    }

    private val nameInput: InputView
    private val colorView: View
    private val emojiView: EmojiView
    private val colorPicker: RecyclerView
    private val emojiPicker: RecyclerView
    private val overlayView: View
    private val actionView: View
    private val button: Button
    private val photoView: ImageView
    private val photoActionView: TextView
    private val previewView: View
    private val previewEmojiView: EmojiView
    private val previewPhotoView: ImageView

    var name: String
        get() = nameInput.text
        set(value) {
            nameInput.text = value
            notifyChange()
        }

    var emoji: CharSequence
        get() = emojiView.getEmoji()
        set(value) {
            if (emojiView.setEmoji(value, Color.TRANSPARENT)) {
                previewEmojiView.setEmoji(value, Color.TRANSPARENT)
                HapticHelper.selection(context)
                emojiView.runAnimation(uikit.R.anim.scale_switch)
                previewEmojiView.runAnimation(uikit.R.anim.scale_switch)
                notifyChange()
            }
        }

    /**
     * Путь к фотографии на аватаре либо null — тогда показывается эмодзи.
     */
    var photoPath: String? = null
        set(value) {
            if (value != field) {
                field = value
                applyPhoto()
            }
        }

    var color: Int = Color.TRANSPARENT
        set(value) {
            if (value != field) {
                colorAdapter.activeColor = value
                colorView.backgroundTintList = value.stateList
                previewView.backgroundTintList = value.stateList
                scrollToColor(value)
                field = value
                notifyChange()
            }
        }

    init {
        inflate(context, R.layout.view_editor_label, this)
        nameInput = findViewById(R.id.label_name_input)
        nameInput.setOnDoneActionListener { done() }

        colorView = findViewById(R.id.label_color)
        colorView.setOnClickListener { nameInput.hideKeyboard() }

        emojiView = findViewById(R.id.label_emoji)

        photoView = findViewById(R.id.label_photo)
        // Фотография лежит с отступом внутри подложки, поэтому цвет метки виден
        // вокруг неё рамкой. Обрезается по собственному фону той же формы.
        photoView.clipToOutline = true
        colorView.clipToOutline = true

        previewView = findViewById(R.id.label_preview)
        previewView.clipToOutline = true
        previewEmojiView = findViewById(R.id.label_preview_emoji)
        previewPhotoView = findViewById(R.id.label_preview_photo)
        previewPhotoView.clipToOutline = true

        photoActionView = findViewById(R.id.label_photo_action)
        photoActionView.setOnClickListener {
            if (photoPath.isNullOrBlank()) {
                doOnPickPhoto?.invoke()
            } else {
                doOnRemovePhoto?.invoke()
            }
        }

        colorPicker = findViewById(R.id.label_color_picker)
        emojiPicker = findViewById(R.id.label_emoji_picker)

        overlayView = findViewById(R.id.label_overlay)
        overlayView.setBackgroundColor(context.backgroundPageColor.withAlpha(.68f))
        overlayView.setOnClickListener { nameInput.hideKeyboard() }

        actionView = findViewById(R.id.label_action)
        actionView.background.alpha = 0

        button = findViewById(R.id.label_button)
        button.setOnClickListener { done() }

        applyColorPicker()
        applyEmojiPicker()

        nameInput.doOnTextChange = {
            button.isEnabled = it.isNotEmpty()
        }

        context.useAttributes(attrs, R.styleable.LabelEditorView) {
            button.text = it.getString(R.styleable.LabelEditorView_android_button)
        }
    }

    /**
     * Показывает фото поверх эмодзи либо возвращает эмодзи, если фото нет.
     *
     * Файл декодируется синхронно: [com.tonapps.wallet.data.settings.SettingsRepository]
     * хранит его уже ужатым до 512 px, так что чтение занимает единицы миллисекунд.
     */
    private fun applyPhoto() {
        val path = photoPath
        val bitmap = if (path.isNullOrBlank()) null else BitmapFactory.decodeFile(path)
        if (bitmap == null) {
            photoView.setImageDrawable(null)
            previewPhotoView.setImageDrawable(null)
            photoView.visibility = View.GONE
            previewPhotoView.visibility = View.GONE
            emojiView.visibility = View.VISIBLE
            previewEmojiView.visibility = View.VISIBLE
            photoActionView.setText(Localization.wallet_avatar_choose_photo)
        } else {
            photoView.setImageBitmap(bitmap)
            previewPhotoView.setImageBitmap(bitmap)
            photoView.visibility = View.VISIBLE
            previewPhotoView.visibility = View.VISIBLE
            emojiView.visibility = View.GONE
            previewEmojiView.visibility = View.GONE
            photoActionView.setText(Localization.wallet_avatar_remove_photo)
        }
    }

    /**
     * На низких экранах и при крупном системном шрифте всей высоты не хватает, и
     * сетка эмодзи схлопывается до нуля. Тогда крупное превью убирается — но только
     * если фотографии нет: без неё оно дублирует эмодзи, которое и так видно в
     * квадратике у поля имени. С фотографией превью остаётся, ради него всё и нужно.
     *
     * @return true, если видимость изменилась и требуется повторный замер
     */
    private fun adjustPreviewForSpace(): Boolean {
        if (!photoPath.isNullOrBlank()) {
            if (previewView.visibility != View.VISIBLE) {
                previewView.visibility = View.VISIBLE
                return true
            }
            return false
        }
        val gridHeight = emojiPicker.measuredHeight
        if (previewView.visibility == View.VISIBLE && gridHeight < MIN_EMOJI_GRID_HEIGHT.dp) {
            previewView.visibility = View.GONE
            return true
        }
        if (previewView.visibility == View.GONE &&
            gridHeight - PREVIEW_BLOCK_HEIGHT.dp >= MIN_EMOJI_GRID_HEIGHT.dp
        ) {
            previewView.visibility = View.VISIBLE
            return true
        }
        return false
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        super.onMeasure(widthMeasureSpec, heightMeasureSpec)
        if (adjustPreviewForSpace()) {
            super.onMeasure(widthMeasureSpec, heightMeasureSpec)
        }
    }

    private fun scrollToColor(color: Int) {
        val index = WalletColor.all.indexOf(color)
        if (index >= 0) {
            colorPicker.scrollToPosition(index)
        }
    }

    override fun dispatchApplyWindowInsets(insets: WindowInsets): WindowInsets {
        val insetsCompat = WindowInsetsCompat.toWindowInsetsCompat(insets)
        val navigationInsets = insetsCompat.getInsets(WindowInsetsCompat.Type.navigationBars())
        applyEmojiMargin(navigationInsets.bottom)
        return super.dispatchApplyWindowInsets(insets)
    }

    fun setBottomOffset(offset: Int, progress: Float) {
        setExtrasAlpha(progress)
        actionView.translationY = -offset.toFloat()
    }

    private fun stopScroll() {
        colorPicker.stopScroll()
        emojiPicker.stopScroll()
    }

    fun removeFocus() {
        stopScroll()
        nameInput.hideKeyboard()
    }

    fun focus() {
        nameInput.focus()
    }

    private fun setExtrasAlpha(alpha: Float) {
        actionView.background.alpha = (alpha * 255).toInt()
        overlayView.alpha = alpha
        if (overlayView.alpha == 0f) {
            overlayView.visibility = View.GONE
        } else if (overlayView.visibility == View.GONE) {
            overlayView.visibility = View.VISIBLE
            stopScroll()
        }
    }

    private fun applyColorPicker() {
        colorPicker.adapter = colorAdapter
        colorPicker.layoutManager = object : LinearLayoutManager(context, RecyclerView.HORIZONTAL, false) {

            override fun onLayoutCompleted(state: RecyclerView.State) {
                super.onLayoutCompleted(state)
                val firstVisible = findFirstVisibleItemPosition()
                val lastVisible = findLastVisibleItemPosition()
                val count = (lastVisible - firstVisible) + 1
                if (emojiPicker.layoutManager == null) {
                    emojiPicker.layoutManager = GridLayoutManager(context, count)
                }
            }
        }
    }

    private fun applyEmojiPicker() {
        emojiPicker.adapter = emojiAdapter
    }

    private fun applyEmojiMargin(bottom: Int) {
        val params = emojiPicker.layoutParams as MarginLayoutParams
        if (params.bottomMargin != bottom) {
            params.bottomMargin = bottom
            emojiPicker.layoutParams = params
        }
    }

    suspend fun loadEmoji() = withContext(Dispatchers.IO) {
        val emojis = Emoji.get(context)
        withContext(Dispatchers.Main) {
            emojiAdapter.submitList(emojis)
        }
    }

    private fun done() {
        if (name.isBlank()) {
            return
        }
        removeFocus()
        doOnDone?.invoke(name, emoji.toString(), color)
    }

    private fun notifyChange() {
        doOnChange?.invoke(name, emoji.toString(), color)
    }

    private companion object {
        private const val MIN_EMOJI_GRID_HEIGHT = 120
        private const val PREVIEW_BLOCK_HEIGHT = 128
    }
}