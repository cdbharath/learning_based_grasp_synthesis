import rospy
import cv2
import numpy as np
import cv_bridge
from enum import Enum
from sensor_msgs.msg import Image


class GraspMaskMode(Enum):
    '''
    Different modes for grasp mask.
    
    ALL_ROTATIONS: Calculates the grasp for all the rotations and returns the best one.
    MAJOR_COMPONENT_IMAGE: Calculates the grasp only for the major component of the image.
    MAJOR_COMPONENT_MASK: Calculates the grasp for the major component of each mask.
    '''
    ALL_ROTATIONS = 1
    MAJOR_COMPONENT_IMAGE = 2
    MAJOR_COMPONENT_MASK = 3


class GraspMask:
    '''
    Calulates the grasp for a given depth image using mask based algorithm.
    '''
    def __init__(self):
        self.top_k = 1
        self.bridge = cv_bridge.CvBridge()
        self.grasp_mode = GraspMaskMode.ALL_ROTATIONS
        
        # Define the angles for grasp mask
        self.angles = np.arange(-90, 90, 5).tolist()
        
        # Create masks of different sizes
        self.mask_sizes = [1024/i for i in range(4, 20, 4)]
        self.weights = [1, 2, 3, 4, 5]
        self.generate_masks()

        rospy.Subscriber('/panda_camera/depth/image_raw', Image, self.depth_image_callback)

    
    def generate_masks(self):
        self.masks = []    
        for i in range(len(self.mask_sizes)):
            mask1 = np.ones(( int(self.mask_sizes[i]/5), int(3*self.mask_sizes[i]/5) )) * -1
            mask2 = np.ones(( int(self.mask_sizes[i]/5), int(3*self.mask_sizes[i]/5) )) * 0
            mask3 = np.ones(( int(self.mask_sizes[i]/5), int(3*self.mask_sizes[i]/5) )) * 1
            mask4 = np.ones(( int(self.mask_sizes[i]/5), int(3*self.mask_sizes[i]/5) )) * 0
            mask5 = np.ones(( int(self.mask_sizes[i]/5), int(3*self.mask_sizes[i]/5) )) * -1
            
            mask = np.concatenate((mask1, mask2, mask3, mask4, mask5), axis=0) / ((3/2) * self.mask_sizes[i] * self.mask_sizes[i])
            mask = mask * self.weights[i]
            self.masks.append(mask)


    def depth_image_callback(self, depth_image_msg):
        depth_image = self.bridge.imgmsg_to_cv2(depth_image_msg, desired_encoding='passthrough') 
        self.get_grasp(depth_image)


    def normalize_depth(self, depth_image):
        '''
        Normalizes the depth image to be between 0 and 255.
        
        :param depth_image: The depth image to normalize.
        :return normalized_depth_image: The normalized depth image.
        '''
        normalized_depth_image = (depth_image - np.min(depth_image)) * 255 / (np.max(depth_image) - np.min(depth_image))
        normalized_depth_image = np.uint8(normalized_depth_image)
        
        return normalized_depth_image


    def get_grasp(self, depth_image):
        '''
        Given a depth image, calculates the grasp bounding box
        
        :param depth_image: The depth image to calculate the grasp for.
        :return x, y, angle: The bounding box of the grasp.
        '''
        start = rospy.Time.now()
        
        # Normalize and invert the depth image
        original_depth_image_norm = self.normalize_depth(depth_image)
        original_depth_image_norm_inv = 255 - original_depth_image_norm
        
        largest_contour, largest_contour_image = self.get_largest_contour(original_depth_image_norm.copy())
        
        # Find the major directions of the largest contour
        major_directions, contour_mean, major_components_image = self.get_major_directions(largest_contour, original_depth_image_norm_inv)
        major_component_angle = np.arctan2(major_directions[0, 1], major_directions[0, 0]) * 180 / np.pi
        
        if self.grasp_mode == GraspMaskMode.ALL_ROTATIONS:
            self.angles.append(major_component_angle)
        elif self.grasp_mode == GraspMaskMode.MAJOR_COMPONENT_IMAGE:
            self.angles = [major_component_angle]
        else:
            self.angles = [0]
        
        best_grasps = []
        # Rotate the depth image for the defined angles and apply the masks
        for angle in self.angles:
            # Get affine transformation matrix for rotating the image
            affine_trans = cv2.getRotationMatrix2D(contour_mean, angle, 1.0)
            inv_affine_trans = cv2.getRotationMatrix2D(contour_mean, -angle, 1.0)
    
            # Rotate the depth image
            depth_rotated = cv2.warpAffine((original_depth_image_norm_inv), affine_trans, dsize=(depth_image.shape[1], depth_image.shape[0]))
                    
            # Apply the masks to the rotated depth image
            for mask in self.masks:
                filtered_rotated = depth_rotated.copy()
                filtered_rotated = cv2.filter2D(filtered_rotated, -1, mask)
                # filtered_rotated = self.filter2D(filtered_rotated, mask)
            
                score, x, y = self.calculate_best_grasp(filtered_rotated, inv_affine_trans)
                
                # appends (score, x, y, width, height, angle)
                best_grasps.append((score, x, y, mask.shape[0], mask.shape[1], angle))
            
        # Sort the grasps by score 
        best_grasps = sorted(best_grasps, key=lambda x: x[0], reverse=True)
        
        end = rospy.Time.now()
        rospy.loginfo("[Mask Based Grasp] Time taken: {}".format((end - start) * 0.000000001))
        
        self.visualize_results(original_depth_image_norm_inv, major_components_image, depth_rotated, best_grasps, largest_contour_image)
        return best_grasps[0][1], best_grasps[0][2], best_grasps[0][5]*np.pi/180 + np.pi/2, mask.shape[0]


    def calculate_best_grasp(self, filtered_rotated, inv_affine_trans):
        '''
        Gets the pixel location of the best grasp.
        
        :param filtered_rotated: The filtered rotated depth image.
        :param inv_affine_trans: The inverse affine transformation matrix.
        
        :return score, x, y: The score of the best grasp, and the x and y coordinates of the best grasp.
        '''
        # Get the indices of the max score
        max_idx = np.argmax(filtered_rotated)
        max_loc = np.unravel_index(max_idx, filtered_rotated.shape)
        max_loc_original_frame = inv_affine_trans @ np.array([max_loc[1], max_loc[0], 1])
    
        score = filtered_rotated[max_loc[0], max_loc[1]]
        x = int(max_loc_original_frame[1])
        y = int(max_loc_original_frame[0])    
        
        return score, x, y

    def filter2D(self, depth_image, mask, stride=[1,1]):
        '''
        Applies a 2D filter to the depth image.
        
        :param depth_image: The depth image to apply the filter to.
        :param mask: The mask to apply to the depth image.
        
        :return filtered_image: The filtered image.
        '''
        output_shape = (int((depth_image.shape[0] - mask.shape[0]) / stride[0] + 1), int((depth_image.shape[1] - mask.shape[1]) / stride[1] + 1))
        filtered_image = np.zeros(output_shape)
        
        for i in range(0, output_shape[0]):
            for j in range(0, output_shape[1]):
                # print(i, j)
                filtered_image[i, j] = np.sum(depth_image[i:i+mask.shape[0], j:j+mask.shape[1]] * mask)
        
        return filtered_image.astype(np.uint8)
       
    def get_largest_contour(self, original_depth_image_norm):
        '''
        Gets the largest contour in the depth image.
        
        :param original_depth_image_norm: The normalized depth image.
        
        :return largest_contour: The largest contour in the depth image.
        :return contours_image: The image with the largest contour drawn.
        '''
        # Apply Gaussian blur and threshold the depth image to get the largest contour
        depth_image = original_depth_image_norm.copy()
        depth_image = cv2.GaussianBlur(depth_image, (5, 5), 0)
        _, depth_image = cv2.threshold(depth_image, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)        
        contours, _ = cv2.findContours(depth_image, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        largest_contour = max(contours, key=cv2.contourArea)
        
        # Draw the largest contour
        contours_image = np.ones(depth_image.shape, np.uint8)*255
        contours_image = cv2.drawContours(contours_image, [largest_contour], -1, (0,255,0), 3)
        
        return largest_contour, contours_image
        
        
    def get_major_directions(self, largest_contour, depth_image):
        '''
        Finds major components of the input contour.
        
        :param largest_contour: The contour to find the major components of.
        :param depth_image: The depth image the contour is from.
        
        :return major_directions: The major components of the contour.
        :return mean_flattened_contour: The mean of the contour.
        :return major_components_image: The image with the major components drawn on it.
        '''
        flattened_contour = np.float32(largest_contour.reshape(-1, 2))
        mean_flattened_contour = np.mean(flattened_contour, axis=0)
        centered_contour = flattened_contour - mean_flattened_contour
        covariance_matrix = np.cov(centered_contour, rowvar=False)
        
        eigenvalues, eigenvectors = np.linalg.eig(covariance_matrix)
        sorted_indices = np.argsort(eigenvalues)[: : -1]
        sorted_eigenvecs = eigenvectors[:, sorted_indices]
        
        major_directions = sorted_eigenvecs[:, :self.top_k]
        
        major_components_image = cv2.cvtColor(depth_image.copy(), cv2.COLOR_GRAY2BGR)
        for direction in major_directions.T:
            start = tuple(np.int32(mean_flattened_contour))
            end = tuple(np.int32(mean_flattened_contour + 100 * direction))

            cv2.arrowedLine(major_components_image, start, end, (0, 0, 255), 2)

        return major_directions.T, mean_flattened_contour, major_components_image
        
        
    def angled_rect(self, image, cx, cy, length, width, angle, color=(0, 255, 0)):
        '''
        Draws an angled rectangle on the input image.
        
        :param image: The image to draw the rectangle on.
        :param cx: The x coordinate of the center of the rectangle.
        :param cy: The y coordinate of the center of the rectangle.
        :param length: The length of the rectangle.
        :param width: The width of the rectangle.
        :param angle: The angle of the rectangle.
        :return image: The image with the rectangle drawn on it.
        '''
        # Create a rotated rectangle
        rect = ((cx, cy), (length, width), angle)
        
        # Compute the vertices of the rectangle
        vertices = cv2.boxPoints(rect)
        vertices = np.int0(vertices)
        
        # Draw the rectangle
        image = cv2.drawContours(image, [vertices], 0, color, 2)
        return image
        
        
    def visualize_results(self, original_depth_image_norm_inv, major_components_image, filtered_rotated, best_grasps, largest_contour_image):
        '''
        Visualizes the results of the grasp detection.
        
        :param original_depth_image_norm_inv: The original depth image.
        :param major_components_image: The image with the major components drawn on it.
        :param filtered_rotated: The filtered rotated depth image.
        :param best_grasps: The best grasps.
        :param angle: The angle of the contour.
        '''
        original_depth_image_norm_inv = cv2.cvtColor(original_depth_image_norm_inv, cv2.COLOR_GRAY2BGR)
        for i, grasp in enumerate(best_grasps[:5]):        
            original_depth_image_norm_inv = cv2.circle(original_depth_image_norm_inv, (grasp[2], grasp[1]), 3, (255, 0, 0), -1)
            original_depth_image_norm_inv = self.angled_rect(original_depth_image_norm_inv, grasp[2], grasp[1], grasp[3], grasp[4], grasp[5] + 90)
        
        original_depth_image_norm_inv = cv2.circle(original_depth_image_norm_inv, (best_grasps[0][2], best_grasps[0][1]), 3, (255, 0, 0), -1)
        original_depth_image_norm_inv = self.angled_rect(original_depth_image_norm_inv, best_grasps[0][2], best_grasps[0][1], 
                                                         best_grasps[0][3], best_grasps[0][4], best_grasps[0][5] + 90, color=(0, 0, 255))

        cv2.imshow('major_components', major_components_image)
        cv2.imshow('filtered_rotated', filtered_rotated)
        cv2.imshow('grasp_results', original_depth_image_norm_inv)
        # cv2.imshow('largest contour image', largest_contour_image)
        cv2.waitKey(0)
        cv2.destroyAllWindows()