package com.tonapps.tonkeeper.ui.component.label

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import uikit.extensions.dp

/**
 * Кадрирование фотографии круглым видоискателем: снимок двигается пальцем и
 * масштабируется щипком, видимой остаётся область внутри круга.
 *
 * Изображение всегда покрывает круг целиком — и масштаб, и сдвиг ограничиваются
 * так, чтобы пустых участков внутри видоискателя не появлялось.
 */
class PhotoCropView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0
) : View(context, attrs, defStyle) {

    private var bitmap: Bitmap? = null

    private val imageMatrix = Matrix()
    private val matrixValues = FloatArray(9)
    private val mappedRect = RectF()
    private val cropRect = RectF()
    private val overlayPath = Path()

    private val overlayPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = OVERLAY_COLOR
    }
    private val borderPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = BORDER_WIDTH.dp.toFloat()
        color = Color.WHITE
    }
    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)

    private var lastTouchX = 0f
    private var lastTouchY = 0f
    private var minScale = 1f

    private val scaleDetector = ScaleGestureDetector(
        context,
        object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                applyScale(detector.scaleFactor, detector.focusX, detector.focusY)
                return true
            }
        }
    )

    fun setBitmap(value: Bitmap) {
        bitmap = value
        resetMatrix()
        invalidate()
    }

    /**
     * Возвращает содержимое видоискателя квадратом [size] × [size].
     * Обрезка по кругу делается при отображении, поэтому здесь квадрат:
     * так снимок остаётся пригодным, если форма аватара когда-нибудь изменится.
     */
    fun crop(size: Int): Bitmap? {
        val source = bitmap ?: return null
        if (cropRect.isEmpty) {
            return null
        }
        val output = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(output)
        val matrix = Matrix(imageMatrix)
        matrix.postTranslate(-cropRect.left, -cropRect.top)
        val scale = size / cropRect.width()
        matrix.postScale(scale, scale)
        canvas.drawBitmap(source, matrix, bitmapPaint)
        return output
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        val side = minOf(w, h) - VIEWPORT_MARGIN.dp * 2
        val left = (w - side) / 2f
        val top = (h - side) / 2f
        cropRect.set(left, top, left + side, top + side)
        resetMatrix()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val source = bitmap ?: return
        canvas.drawBitmap(source, imageMatrix, bitmapPaint)

        overlayPath.reset()
        overlayPath.addRect(0f, 0f, width.toFloat(), height.toFloat(), Path.Direction.CW)
        overlayPath.addCircle(cropRect.centerX(), cropRect.centerY(), cropRect.width() / 2f, Path.Direction.CCW)
        canvas.drawPath(overlayPath, overlayPaint)
        canvas.drawCircle(cropRect.centerX(), cropRect.centerY(), cropRect.width() / 2f, borderPaint)
    }

    @Suppress("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        scaleDetector.onTouchEvent(event)
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                parent?.requestDisallowInterceptTouchEvent(true)
                lastTouchX = event.x
                lastTouchY = event.y
            }
            MotionEvent.ACTION_MOVE -> if (!scaleDetector.isInProgress) {
                imageMatrix.postTranslate(event.x - lastTouchX, event.y - lastTouchY)
                lastTouchX = event.x
                lastTouchY = event.y
                clampMatrix()
                invalidate()
            }
            MotionEvent.ACTION_POINTER_UP -> {
                lastTouchX = event.x
                lastTouchY = event.y
            }
        }
        return true
    }

    private fun applyScale(factor: Float, focusX: Float, focusY: Float) {
        val current = currentScale()
        val target = (current * factor).coerceIn(minScale, minScale * MAX_ZOOM)
        val applied = target / current
        imageMatrix.postScale(applied, applied, focusX, focusY)
        clampMatrix()
        invalidate()
    }

    private fun currentScale(): Float {
        imageMatrix.getValues(matrixValues)
        return matrixValues[Matrix.MSCALE_X]
    }

    private fun resetMatrix() {
        val source = bitmap ?: return
        if (cropRect.isEmpty) {
            return
        }
        minScale = maxOf(
            cropRect.width() / source.width,
            cropRect.height() / source.height
        )
        imageMatrix.reset()
        imageMatrix.postScale(minScale, minScale)
        imageMatrix.postTranslate(
            cropRect.centerX() - source.width * minScale / 2f,
            cropRect.centerY() - source.height * minScale / 2f
        )
    }

    /** Не даёт снимку отойти так, чтобы внутри круга появилась пустота. */
    private fun clampMatrix() {
        val source = bitmap ?: return
        mappedRect.set(0f, 0f, source.width.toFloat(), source.height.toFloat())
        imageMatrix.mapRect(mappedRect)

        var dx = 0f
        var dy = 0f
        if (mappedRect.left > cropRect.left) {
            dx = cropRect.left - mappedRect.left
        } else if (mappedRect.right < cropRect.right) {
            dx = cropRect.right - mappedRect.right
        }
        if (mappedRect.top > cropRect.top) {
            dy = cropRect.top - mappedRect.top
        } else if (mappedRect.bottom < cropRect.bottom) {
            dy = cropRect.bottom - mappedRect.bottom
        }
        if (dx != 0f || dy != 0f) {
            imageMatrix.postTranslate(dx, dy)
        }
    }

    private companion object {
        private const val OVERLAY_COLOR = 0xB3000000.toInt()
        private const val BORDER_WIDTH = 2
        private const val VIEWPORT_MARGIN = 24
        private const val MAX_ZOOM = 5f
    }
}
