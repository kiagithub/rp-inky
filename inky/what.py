"""Inky wHAT e-Ink display driver."""
from . import inky


class InkyWHAT(inky.Inky):
    """Inky wHAT e-Ink Display Driver.

    :Example: ::

        >>> from inky import InkyWHAT
        >>> display = InkyWHAT('red')
        >>> display.set_border(display.BLACK)
        >>> for x in range(display.WIDTH):
        >>>     for y in range(display.HEIGHT):
        >>>         display.set_pixel(x, y, display.RED)
        >>> display.show()
    """

    WIDTH = 400
    HEIGHT = 300

    WHITE = 0
    BLACK = 1
    RED = 2
    YELLOW = 2

    def __init__(self, colour='black'):
        """Initialise an Inky wHAT Display.

        :param str colour: one of 'red', 'black' or 'yellow', default: 'black'.
        """
        inky.Inky.__init__(
            self,
            resolution=(self.WIDTH, self.HEIGHT),
            colour=colour,
            h_flip=False,
            v_flip=False)
