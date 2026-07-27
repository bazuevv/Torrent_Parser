package uikit.widget

import android.content.Context
import android.text.Layout
import android.text.SpannableStringBuilder
import android.text.TextUtils
import android.util.AttributeSet
import androidx.appcompat.widget.AppCompatTextView

/**
 * Single-line text view that keeps an optional trailing badge fully visible while ellipsizing
 * the primary text in front of it, e.g. `[img] Token Na… [badge]`.
 *
 * The available width is only known at measure time, so the badge width is reserved there and the
 * primary text is truncated to fit the remaining space instead of letting the badge be clipped.
 */
class BadgeTextView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = android.R.attr.textViewStyle,
) : AppCompatTextView(context, attrs, defStyle) {

    private var primaryText: CharSequence = ""
    private var badge: CharSequence? = null
    private var lastWidth = -1

    fun setTextWithBadge(text: CharSequence?, badge: CharSequence?) {
        primaryText = text ?: ""
        this.badge = badge?.takeIf { it.isNotEmpty() }
        lastWidth = -1
        requestLayout()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = MeasureSpec.getSize(widthMeasureSpec)
        if (width != lastWidth) {
            lastWidth = width
            applyText(width)
        }
        super.onMeasure(widthMeasureSpec, heightMeasureSpec)
    }

    private fun applyText(width: Int) {
        val available = width - compoundPaddingLeft - compoundPaddingRight
        val badge = badge
        val badgeWidth = if (badge == null) 0f else Layout.getDesiredWidth(badge, paint)
        val nameMaxWidth = available - badgeWidth
        val name = if (available > 0 && nameMaxWidth > 0) {
            TextUtils.ellipsize(primaryText, paint, nameMaxWidth, TextUtils.TruncateAt.END)
        } else {
            primaryText
        }
        val result = if (badge == null) {
            name
        } else {
            SpannableStringBuilder(name).append(badge)
        }
        if (!TextUtils.equals(text, result)) {
            text = result
        }
    }
}
