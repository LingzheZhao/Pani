package com.example.android.camera2.basic.fragments

import java.util.concurrent.ArrayBlockingQueue

class ListenerArrayBlockingQueue<E>(capacity: Int) : ArrayBlockingQueue<E>(capacity) {

    private var listener: ((E) -> Unit)? = null

    fun setOnItemAddedListener(listener: (E) -> Unit) {
        this.listener = listener
    }

    override fun offer(e: E): Boolean {
        val result = super.offer(e)
        if (result) {
            listener?.invoke(e)
        }
        return result
    }

    override fun put(e: E) {
        super.put(e)
        listener?.invoke(e)
    }
}